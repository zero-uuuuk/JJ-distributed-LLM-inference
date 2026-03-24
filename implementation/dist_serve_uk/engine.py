"""
engine.py
---------
OPT-6.7B 모델 로딩 및 forward pass 래퍼.

설계 방식: KV Cache padding
  요청마다 past_seq_len이 달라 KV Cache를 배치 텐서로 직접 합칠 수 없다.
  이를 해결하기 위해 KV Cache의 seq_len 차원을 zero-padding으로 맞춘 뒤
  배치로 합쳐 단일 forward pass를 실행한다.
  attention_mask로 padding 위치를 마스킹하여 모델이 무시하도록 한다.

  forward pass 후 반환된 past_key_values를 요청별로 분리하여 저장하고,
  다음 iteration에서 재사용한다. input_ids는 항상 next token 1개만 전달한다.

공개 인터페이스:
  - OPTEngine.prefill(input_ids_list)
      각 요청을 개별 prefill하여 KV Cache를 확보한다. (준비 단계)
      반환: (kv_list, next_token_list)

  - OPTEngine.run_iteration(next_token_list, kv_list, prefill_seq=None)
      decoding 요청들과 (선택적으로) prefill 요청 1개를
      KV Cache padding 후 단일 forward pass로 실행한다.
      반환: (next_token_list, kv_list, prefill_kv, prefill_next_token)
"""

import torch
from transformers import AutoModelForCausalLM


class OPTEngine:
    def __init__(self, model_name: str = "facebook/opt-6.7b", device: str = "cuda"):
        """모델을 fp16으로 로드한다."""
        self.device = device
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.float16,
            device_map=device,
        )
        self.model.eval()
        self.vocab_size = self.model.config.vocab_size
        self.n_layers = self.model.config.num_hidden_layers
        self.n_heads = self.model.config.num_attention_heads
        self.head_dim = self.model.config.hidden_size // self.n_heads

    # ------------------------------------------------------------------
    # 준비 단계 (측정 제외)
    # ------------------------------------------------------------------

    def prefill(self, input_ids_list: list[torch.Tensor]) -> tuple[list, list[torch.Tensor]]:
        """
        각 요청을 개별적으로 prefill하여 KV Cache와 첫 번째 decoding 입력 토큰을 확보한다.
        요청마다 past_seq_len이 다르므로 개별 forward pass로 처리한다.

        Args:
            input_ids_list: 요청별 1D token id 텐서 리스트. shape: (seq_len,) each.

        Returns:
            kv_list        : 요청별 KV Cache 리스트.
                             각 원소는 tuple of (key, value) per layer.
                             key/value shape: (1, n_heads, seq_len, head_dim)
            next_token_list: 요청별 첫 번째 decoding 입력 토큰 리스트. shape: (1,) each.
                             prefill forward pass의 출력 토큰 (logits argmax).
        """
        kv_list = []
        next_token_list = []
        with torch.no_grad():
            for input_ids in input_ids_list:
                out = self.model(
                    input_ids=input_ids.unsqueeze(0).to(self.device),
                    use_cache=True,
                )
                kv_list.append(out.past_key_values)
                # 첫 번째 decoding 입력: prefill forward pass의 출력 토큰
                next_token = out.logits[0, -1, :].argmax(dim=-1, keepdim=True)
                next_token_list.append(next_token)
        return kv_list, next_token_list

    # ------------------------------------------------------------------
    # 측정 구간 — 단일 iteration forward pass
    # ------------------------------------------------------------------

    def run_iteration(
        self,
        next_token_list: list[torch.Tensor],
        kv_list: list,
        prefill_seq: torch.Tensor | None = None,
    ) -> tuple[list[torch.Tensor], list, tuple | None]:
        """
        decoding 요청들과 (선택적으로) prefill 요청 1개를
        KV Cache padding 후 단일 forward pass로 실행한다.

        KV Cache padding 전략:
          - 각 요청의 past_seq_len을 max_past_seq_len에 맞춰 left zero-padding
          - attention_mask: padding 위치 0, 실제 토큰 위치 1
          - prefill 요청은 past_kv가 없으므로 zero KV Cache로 채움

        Args:
            next_token_list : decoding 요청별 next input token. shape: (1,) each.
            kv_list         : 요청별 KV Cache 리스트 (prefill 반환값).
            prefill_seq     : prefill 요청의 전체 입력 시퀀스. shape: (prefill_len,).
                              None이면 decoding 요청만 처리한다.

        Returns:
            next_token_list    : 갱신된 요청별 next token id 리스트.
            kv_list            : 갱신된 요청별 KV Cache 리스트.
            prefill_kv         : prefill 요청의 KV Cache. prefill_seq가 None이면 None.
            prefill_next_token : prefill 요청의 출력 토큰. prefill_seq가 None이면 None.
        """
        n_decode = len(next_token_list)

        # decoding 요청의 past_seq_len 목록
        # kv shape per layer: (1, n_heads, past_seq_len, head_dim)
        past_seq_lens = [kv[0][0].shape[2] for kv in kv_list]

        if prefill_seq is not None:
            # prefill 요청은 past_kv 없음 → past_seq_len=0
            past_seq_lens.append(0)

        max_past = max(past_seq_lens)

        # new_seq_len: decoding=1, prefill=전체 시퀀스 길이
        new_seq_lens = [1] * n_decode
        if prefill_seq is not None:
            new_seq_lens.append(prefill_seq.shape[0])

        max_new = max(new_seq_lens)
        batch_size = len(past_seq_lens)

        # --- input_ids 구성 (left-padding) ---
        input_ids = torch.zeros(batch_size, max_new, dtype=torch.long, device=self.device)
        for i, tok in enumerate(next_token_list):
            # decoding 요청: 마지막 위치에 next token 1개
            input_ids[i, -1] = tok.to(self.device)
        if prefill_seq is not None:
            seq_len = prefill_seq.shape[0]
            input_ids[n_decode, max_new - seq_len:] = prefill_seq.to(self.device)

        # --- attention_mask 구성 ---
        # 총 시퀀스 길이 = past_seq_len + new_seq_len
        max_total = max_past + max_new
        attention_mask = torch.zeros(batch_size, max_total, dtype=torch.long, device=self.device)
        for i, (past_len, new_len) in enumerate(zip(past_seq_lens, new_seq_lens)):
            total_len = past_len + new_len
            attention_mask[i, max_total - total_len:] = 1

        # --- KV Cache padding 및 배치 병합 ---
        padded_kv = self._pad_and_merge_kv(kv_list, max_past, prefill_seq is not None)

        # --- position_ids 구성 ---
        # 각 요청의 new token 위치 인덱스 = past_seq_len ~ past_seq_len + new_seq_len - 1
        position_ids = torch.zeros(batch_size, max_new, dtype=torch.long, device=self.device)
        for i, (past_len, new_len) in enumerate(zip(past_seq_lens, new_seq_lens)):
            pad_new = max_new - new_len
            position_ids[i, pad_new:] = torch.arange(
                past_len, past_len + new_len, device=self.device
            )

        with torch.no_grad():
            out = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=padded_kv,
                use_cache=True,
            )

        return self._split_outputs(out, n_decode, past_seq_lens, new_seq_lens)

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _pad_and_merge_kv(
        self, kv_list: list, max_past: int, has_prefill: bool
    ) -> tuple:
        """
        요청별 KV Cache를 max_past에 맞춰 left zero-padding 후 배치로 합친다.
        prefill 요청은 past_kv가 없으므로 전체가 zero인 KV Cache를 추가한다.

        반환: tuple of (key, value) per layer.
              key/value shape: (batch, n_heads, max_past, head_dim)
        """
        merged = []
        for layer_idx in range(self.n_layers):
            keys, values = [], []
            for kv in kv_list:
                k, v = kv[layer_idx]  # shape: (1, n_heads, past_seq_len, head_dim)
                past_len = k.shape[2]
                pad_len = max_past - past_len
                if pad_len > 0:
                    # left zero-padding으로 seq_len 차원을 max_past에 맞춤
                    pad = torch.zeros(
                        1, self.n_heads, pad_len, self.head_dim,
                        dtype=k.dtype, device=self.device
                    )
                    k = torch.cat([pad, k], dim=2)
                    v = torch.cat([pad, v], dim=2)
                keys.append(k)
                values.append(v)

            if has_prefill:
                # prefill 요청: past_kv 없음 → zero KV Cache
                # dtype은 기존 KV Cache와 동일하게 맞춤
                kv_dtype = kv_list[0][layer_idx][0].dtype
                zero = torch.zeros(
                    1, self.n_heads, max_past, self.head_dim,
                    dtype=kv_dtype, device=self.device
                )
                keys.append(zero)
                values.append(zero)

            merged.append((
                torch.cat(keys, dim=0),   # shape: (batch, n_heads, max_past, head_dim)
                torch.cat(values, dim=0),
            ))

        return tuple(merged)

    def _split_outputs(
        self,
        out,
        n_decode: int,
        past_seq_lens: list[int],
        new_seq_lens: list[int],
    ) -> tuple[list[torch.Tensor], list, tuple | None]:
        """
        forward pass 출력에서 요청별 next_token과 KV Cache를 분리한다.

        past_key_values shape per layer: (batch, n_heads, max_past + max_new, head_dim)
        각 요청의 실제 KV Cache는 총 시퀀스 길이(past + new)만큼 뒤에서 슬라이싱한다.
        """
        n_layers = len(out.past_key_values)

        # next token: 각 요청의 logits 마지막 위치에서 argmax
        next_token_list = [
            out.logits[i, -1, :].argmax(dim=-1, keepdim=True)
            for i in range(n_decode)
        ]

        # decoding 요청별 KV Cache 분리
        updated_kv_list = []
        for i in range(n_decode):
            total_len = past_seq_lens[i] + new_seq_lens[i]
            kv = tuple(
                (
                    out.past_key_values[l][0][i:i+1, :, -total_len:, :],
                    out.past_key_values[l][1][i:i+1, :, -total_len:, :],
                )
                for l in range(n_layers)
            )
            updated_kv_list.append(kv)

        # prefill 요청 next token 및 KV Cache 분리 (있는 경우)
        prefill_next_token = None
        prefill_kv = None
        if len(past_seq_lens) > n_decode:
            i = n_decode
            total_len = past_seq_lens[i] + new_seq_lens[i]
            # prefill forward pass의 출력 토큰 (입력의 마지막 토큰이 아닌 모델 출력)
            prefill_next_token = out.logits[i, -1, :].argmax(dim=-1, keepdim=True)
            prefill_kv = tuple(
                (
                    out.past_key_values[l][0][i:i+1, :, -total_len:, :],
                    out.past_key_values[l][1][i:i+1, :, -total_len:, :],
                )
                for l in range(n_layers)
            )

        return next_token_list, updated_kv_list, prefill_kv, prefill_next_token
