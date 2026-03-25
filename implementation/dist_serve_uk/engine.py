"""
engine.py
---------
OPT-6.7B 모델 로딩 및 forward pass 래퍼.

설계 방식: KV Cache padding
  요청마다 past_seq_len이 달라 KV Cache를 배치 텐서로 직접 합칠 수 없다.
  이를 해결하기 위해 KV Cache의 seq_len 차원을 zero-padding으로 맞춘 뒤
  배치로 합쳐 단일 forward pass를 실행한다.
  attention_mask로 padding 위치를 마스킹하여 모델이 무시하도록 한다.

KV Cache 내부 표현 (transformers 5.x DynamicCache):
  DynamicCache.layers[l].keys  : shape (batch, n_heads, seq_len, head_dim)
  DynamicCache.layers[l].values: shape (batch, n_heads, seq_len, head_dim)
  본 코드에서는 요청별로 tuple of (key, value) per layer 형태로 관리한다.
  key/value shape: (1, n_heads, seq_len, head_dim)

공개 인터페이스:
  - OPTEngine.prefill(input_ids_list)  → (kv_list, next_token_list)
  - OPTEngine.run_iteration(next_token_list, kv_list, prefill_seq=None)
      → (next_token_list, kv_list, prefill_kv, prefill_next_token)
"""

import torch
from transformers import AutoModelForCausalLM
from transformers.cache_utils import DynamicCache


def _cache_to_kv(cache: DynamicCache) -> tuple:
    """DynamicCache (batch=1) → tuple of (key, value) per layer."""
    return tuple(
        (layer.keys, layer.values)
        for layer in cache.layers
    )


def _kv_list_to_cache(kv_list: list, n_layers: int, n_heads: int, head_dim: int,
                      max_past: int, has_prefill: bool, device: str) -> DynamicCache:
    """
    요청별 KV Cache를 max_past에 맞춰 left zero-padding 후 배치로 합쳐
    DynamicCache로 반환한다.
    prefill 요청은 past_kv가 없으므로 zero KV Cache를 추가한다.
    """
    kv_dtype = kv_list[0][0][0].dtype  # (request 0, layer 0, key)

    cache = DynamicCache()
    # layers 리스트를 직접 구성
    from transformers.cache_utils import DynamicLayer
    cache.layers = []

    for l in range(n_layers):
        keys, values = [], []
        for kv in kv_list:
            k, v = kv[l]  # (1, n_heads, past_seq_len, head_dim)
            pad_len = max_past - k.shape[2]
            if pad_len > 0:
                pad = torch.zeros(1, n_heads, pad_len, head_dim, dtype=k.dtype, device=device)
                k = torch.cat([pad, k], dim=2)
                v = torch.cat([pad, v], dim=2)
            keys.append(k)
            values.append(v)

        if has_prefill:
            zero = torch.zeros(1, n_heads, max_past, head_dim, dtype=kv_dtype, device=device)
            keys.append(zero)
            values.append(zero)

        layer = DynamicLayer()
        layer.keys = torch.cat(keys, dim=0)
        layer.values = torch.cat(values, dim=0)
        cache.layers.append(layer)

    return cache


class OPTEngine:
    def __init__(self, model_name: str = "facebook/opt-6.7b", device: str = "cuda"):
        """모델을 fp16으로 로드한다."""
        self.device = device
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            dtype=torch.float16,
            device_map=device,
        )
        self.model.eval()
        self.vocab_size = self.model.config.vocab_size
        self.n_layers = self.model.config.num_hidden_layers
        self.n_heads = self.model.config.num_attention_heads
        self.head_dim = self.model.config.hidden_size // self.n_heads

    def prefill(self, input_ids_list: list[torch.Tensor]) -> tuple[list, list[torch.Tensor]]:
        """
        각 요청을 개별 prefill하여 KV Cache와 첫 번째 decoding 입력 토큰을 확보한다.

        Returns:
            kv_list        : 요청별 tuple of (key, value) per layer 리스트.
            next_token_list: 요청별 첫 번째 decoding 입력 토큰. shape: (1,) each.
        """
        kv_list = []
        next_token_list = []
        with torch.no_grad():
            for input_ids in input_ids_list:
                out = self.model(
                    input_ids=input_ids.unsqueeze(0).to(self.device),
                    use_cache=True,
                )
                kv_list.append(_cache_to_kv(out.past_key_values))
                next_token_list.append(out.logits[0, -1, :].argmax(dim=-1, keepdim=True))
        return kv_list, next_token_list

    def run_iteration(
        self,
        next_token_list: list[torch.Tensor],
        kv_list: list,
        prefill_seq: torch.Tensor | None = None,
    ) -> tuple[list[torch.Tensor], list, tuple | None, torch.Tensor | None]:
        """
        decoding 요청들과 (선택적으로) prefill 요청 1개를
        KV Cache padding 후 단일 forward pass로 실행한다.

        Returns:
            next_token_list    : 갱신된 요청별 next token id 리스트.
            kv_list            : 갱신된 요청별 KV Cache 리스트.
            prefill_kv         : prefill 요청의 KV Cache. prefill_seq가 None이면 None.
            prefill_next_token : prefill 요청의 출력 토큰. prefill_seq가 None이면 None.
        """
        n_decode = len(next_token_list)

        # kv[l] = (key, value), key shape: (1, n_heads, past_seq_len, head_dim)
        past_seq_lens = [kv[0][0].shape[2] for kv in kv_list]
        if prefill_seq is not None:
            past_seq_lens.append(0)

        max_past = max(past_seq_lens)
        new_seq_lens = [1] * n_decode
        if prefill_seq is not None:
            new_seq_lens.append(prefill_seq.shape[0])

        max_new = max(new_seq_lens)
        batch_size = len(past_seq_lens)

        # input_ids (left-padding)
        input_ids = torch.zeros(batch_size, max_new, dtype=torch.long, device=self.device)
        for i, tok in enumerate(next_token_list):
            input_ids[i, -1] = tok.to(self.device)
        if prefill_seq is not None:
            seq_len = prefill_seq.shape[0]
            input_ids[n_decode, max_new - seq_len:] = prefill_seq.to(self.device)

        # attention_mask
        max_total = max_past + max_new
        attention_mask = torch.zeros(batch_size, max_total, dtype=torch.long, device=self.device)
        for i, (past_len, new_len) in enumerate(zip(past_seq_lens, new_seq_lens)):
            attention_mask[i, max_total - (past_len + new_len):] = 1

        # position_ids
        position_ids = torch.zeros(batch_size, max_new, dtype=torch.long, device=self.device)
        for i, (past_len, new_len) in enumerate(zip(past_seq_lens, new_seq_lens)):
            pad_new = max_new - new_len
            position_ids[i, pad_new:] = torch.arange(past_len, past_len + new_len, device=self.device)

        padded_cache = _kv_list_to_cache(
            kv_list, self.n_layers, self.n_heads, self.head_dim,
            max_past, prefill_seq is not None, self.device
        )

        with torch.no_grad():
            out = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=padded_cache,
                use_cache=True,
            )

        return self._split_outputs(out, n_decode, past_seq_lens, new_seq_lens)

    def _split_outputs(self, out, n_decode, past_seq_lens, new_seq_lens):
        """forward pass 출력에서 요청별 next_token과 KV Cache를 분리한다."""
        cache = out.past_key_values  # DynamicCache

        next_token_list = [
            out.logits[i, -1, :].argmax(dim=-1, keepdim=True)
            for i in range(n_decode)
        ]

        updated_kv_list = []
        for i in range(n_decode):
            total_len = past_seq_lens[i] + new_seq_lens[i]
            kv = tuple(
                (
                    cache.layers[l].keys[i:i+1, :, -total_len:, :],
                    cache.layers[l].values[i:i+1, :, -total_len:, :],
                )
                for l in range(self.n_layers)
            )
            updated_kv_list.append(kv)

        prefill_next_token = None
        prefill_kv = None
        if len(past_seq_lens) > n_decode:
            i = n_decode
            total_len = past_seq_lens[i] + new_seq_lens[i]
            prefill_next_token = out.logits[i, -1, :].argmax(dim=-1, keepdim=True)
            prefill_kv = tuple(
                (
                    cache.layers[l].keys[i:i+1, :, -total_len:, :],
                    cache.layers[l].values[i:i+1, :, -total_len:, :],
                )
                for l in range(self.n_layers)
            )

        return next_token_list, updated_kv_list, prefill_kv, prefill_next_token
