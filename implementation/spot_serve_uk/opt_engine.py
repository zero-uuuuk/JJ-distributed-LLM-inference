import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
from tqdm import tqdm
import time
import gc

class OPTEngine:
    """
    SpotServe Reference: ParamsClient/src/client/TensorStorage.cc 및 FT-Worker 가중치 관리 방식
    - KV Cache를 레이어별로 추출하여 관리하는 방식을 모방
    - 추론 중단 시점의 상태를 'State Dict'와 'past_key_values'로 캡처하여 이전 가능하게 함
    """
    def __init__(self, model_name="facebook/opt-125m", device="cpu", init_empty=False):
        """
        - model_name: 모델 식별자
        - device: 실행 디바이스 (cpu 등)
        - init_empty: True일 경우 가중치 없이 '뼈대(Architecture)'만 생성 (SpotServe P2P 마이그레이션 PoC 용도)
        """
        self.device = device
        print(f"Loading model {model_name} on {self.device}...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        # 배치 처리를 위한 패딩 토큰 설정 (OPT는 EOS를 패딩으로 사용 가능)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        
        if init_empty:
            # SpotServe Philosophy: 가중치 없이 구조만 로드하여 P2P 주입을 대기
            print(f"Initializing empty model skeleton (Architecture Only): {model_name}...")
            config = AutoConfig.from_pretrained(model_name)
            # from_config는 가중치를 랜덤하게 초기화하여 구조만 생성함
            with torch.device(device):
                self.model = AutoModelForCausalLM.from_config(config)
        else:
            # 일반적인 로딩: 가중치까지 포함하여 로드
            print(f"Loading full pre-trained model: {model_name} on {self.device}...")
            self.model = AutoModelForCausalLM.from_pretrained(model_name).to(device)
        
        self.model.eval()
        
    def get_state_dict(self):
        """
        SpotServe Reference: ParamsClient/src/client/TensorStorage.cc
        - 마이그레이션을 위해 현재 모델의 모든 가중치(Weights)를 캡처하여 반환합니다.
        - Node A(Sender)에서 호출하여 전송할 데이터를 생성합니다.
        """
        return self.model.state_dict()
    
    def load_state_dict(self, state_dict):
        """
        SpotServe Reference: Worker/Runtime State Restoration
        - 전송받은 가중치를 현재 모델에 주입하여 상태를 복구합니다.
        - Node B(Receiver)에서 호출하여 Node A와 동일한 상태로 동기화합니다.
        """
        self.model.load_state_dict(state_dict)
        
    @torch.no_grad()
    def generate_step(self, input_ids, attention_mask=None, past_key_values=None):
        """
        SpotServe Reference: Layer-wise State Management (KV Cache)
        - 모델의 forward pass를 수행하여 다음 토큰과 업데이트된 KV Cache를 얻습니다.
        - use_cache=True: 이전 계산 결과를 재사용하여 추론 속도를 최적화합니다.
        - past_key_values: 마이그레이션 시 전송되는 핵심 데이터로, 이전 문맥 정보를 담고 있습니다.
        """
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            use_cache=True  # SpotServe의 핵심인 KV Cache 메커니즘 활성화
        )
        logits = outputs.logits
        
        # Greedy search 적용: 확률이 가장 높은 다음 토큰 ID를 선택
        next_token_ids = torch.argmax(logits[:, -1, :], dim=-1).unsqueeze(-1)
        
        # 새로운 토큰이 생성됨에 따라 Attention Mask의 길이를 확장
        if attention_mask is not None:
            next_mask = torch.ones((next_token_ids.shape[0], 1), device=self.device)
            attention_mask = torch.cat([attention_mask, next_mask], dim=-1)
            
        # 다음 단계에서 사용할 토큰 ID, 업데이트된 KV Cache, 확장된 마스크 반환
        return next_token_ids, outputs.past_key_values, attention_mask

    def run_inference_batch(self, prompts, max_new_tokens=50, callback=None):
        """
        SpotServe Reference: Stateful Request Migration
        - 프롬프트 배치를 처리하며, 토큰 생성이 진행되는 동안 마이그레이션 신호를 감시합니다.
        - callback: 매 토큰 생성 시 호출되어 마이그레이션 수행 여부를 결정합니다.
        """
        encoding = self.tokenizer(prompts, return_tensors="pt", padding=True).to(self.device)
        input_ids = encoding.input_ids
        attention_mask = encoding.attention_mask
        
        batch_size = input_ids.shape[0]
        past_key_values = None
        all_generated_ids = input_ids # 프롬프트와 생성된 토큰을 합쳐서 관리
        
        current_input_ids = input_ids
        
        pbar = tqdm(total=max_new_tokens, desc=f"Generating Batch", leave=False)
        for i in range(max_new_tokens):
            # 매 스텝마다 마이그레이션 중단 요청이 있는지 확인
            if callback and callback(i, all_generated_ids, past_key_values):
                pbar.close()
                # 마이그레이션에 필요한 모든 상태(Context)를 패키징하여 즉시 반환
                return {
                    "all_generated_ids": all_generated_ids,
                    "attention_mask": attention_mask,
                    "past_key_values": past_key_values,
                    "next_step": i,
                    "batch_size": batch_size
                }
            
            next_token_ids, past_key_values, attention_mask = self.generate_step(
                current_input_ids, attention_mask, past_key_values
            )
            
            all_generated_ids = torch.cat([all_generated_ids, next_token_ids], dim=-1)
            current_input_ids = next_token_ids
            pbar.update(1)
            
        pbar.close()
            
        return {
            "all_generated_ids": all_generated_ids,
            "attention_mask": attention_mask,
            "past_key_values": past_key_values,
            "next_step": max_new_tokens,
            "batch_size": batch_size
        }

    def resume_inference_batch(self, state, max_new_tokens=50, callback=None):
        """
        SpotServe Reference: Context-Aware Resume
        - 전송받은 상태(State)를 기반으로 중단된 지점부터 추론을 재개합니다.
        - KV Cache(past_key_values)를 복구하여 중복 연산 없이 즉시 토큰 생성을 시작합니다.
        """
        past_key_values = state["past_key_values"]
        all_generated_ids = state["all_generated_ids"]
        attention_mask = state["attention_mask"]
        step = state["next_step"]
        batch_size = state["batch_size"]
        
        # 마지막으로 생성된 토큰을 입력값으로 설정
        current_input_ids = all_generated_ids[:, -1:]
        
        print(f"\nResuming batch inference (Size: {batch_size}) from step {step}...")
        pbar = tqdm(total=max_new_tokens, initial=step, desc="Resuming Batch")
        
        for i in range(step, max_new_tokens):
            if callback and callback(i, all_generated_ids, past_key_values):
                break
                
            next_token_ids, past_key_values, attention_mask = self.generate_step(
                current_input_ids, attention_mask, past_key_values
            )
            
            all_generated_ids = torch.cat([all_generated_ids, next_token_ids], dim=-1)
            current_input_ids = next_token_ids
            pbar.update(1)
            
        pbar.close()

        # 마이그레이션 후 최종 결과 디코딩
        results = []
        for j in range(batch_size):
            results.append(self.tokenizer.decode(all_generated_ids[j], skip_special_tokens=True))
        return results

    def cleanup(self):
        """
        모델 자원 및 메모리 명시적 해제.
        SpotServe와 같은 긴 실행 시간을 갖는 시스템에서 메모리 누수를 방지하기 위해 중요합니다.
        """
        model_name = getattr(self, 'model', None)
        if model_name:
            del self.model
        if hasattr(self, 'tokenizer'):
            del self.tokenizer
        
        # 가비지 컬렉션 강제 수행
        gc.collect()
        
        # GPU 사용 시 캐시 비우기 (현재는 CPU 위주이나 확장성을 고려)
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            
        print("[OPTEngine] Model and cache resources have been cleared.")
