import time
import threading
import argparse
from migration_utils import TcpClient, timestamp
from opt_engine import OPTEngine
import torch
import random


def get_migration_trigger(stop_event):
    """
    SpotServe 핵심 전략: 'Termination Notice' 수신 즉시 마이그레이션 시작.
    알림 수신 후 주어지는 고정 유예 기간(30초) 동안 전송을 완료하는 것이 목표입니다.
    """
    notification_delay = 20 # 20초로 고정 (SpotServe PoC 실험의 일관성을 위해)
    
    # 1. 종료 알림이 올 때까지 대기
    time.sleep(notification_delay)
    timestamp("Node A", "SPOT TERMINATION NOTICE")
    print(f"\n[Node A] !!! WARNING: Spot Instance Termination Notice Received !!!")
    print(f"[Node A] Migration MUST be completed within the next 30 seconds.")
    
    # 2. 유예 기간이 시작됨과 동시에 즉시 마이그레이션 명령(stop_event) 전달
    stop_event.set()



def generate_test_prompts(count=200):
    """실험용 결정론적 프롬프트 생성"""
    return [f"Discuss the future of artificial intelligence in the context of global task number {i}, focusing on scalability and performance:" for i in range(count)]

def main():
    """
    SpotServe Reference: elastic-switch/scheduler/scheduler.py
    - Scheduler의 `run()` 루프에서 Spot Termination을 감지하고 마이그레이션을 트리거하는 로직을 모방
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest_ip", type=str, default="127.0.0.1", help="Destination instance IP")
    parser.add_argument("--port", type=int, default=10051, help="Destination port")
    parser.add_argument("--mode", type=str, default="spotserve", choices=["spotserve", "naive"], help="Migration mode: spotserve (P2P State) vs naive (Cold Start)")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device to use (cpu, cuda)")
    args = parser.parse_args()

    # 재현성을 위한 시드 고정
    random.seed(42)
    torch.manual_seed(42)

    # 1. 모델 및 추론 엔진 초기화
    engine = OPTEngine(device=args.device)

    # 실험용 프롬프트 생성 (함수화하여 코드 위치 최적화)
    all_prompts = generate_test_prompts(200)
    batch_size = 100
    total_samples = len(all_prompts)
    
    # 2. 마이그레이션 트리거 이벤트 및 감시 스레드 설정
    # Spot 인스턴스의 비정기적인 종료 알림(Notice) 상황을 시뮬레이션하기 위해
    # 백그라운드 스레드에서 랜덤한 시간에 중단 신호를 발생시킵니다.
    stop_event = threading.Event()
    trigger_thread = threading.Thread(target=get_migration_trigger, args=(stop_event,))
    trigger_thread.daemon = True
    trigger_thread.start()


    # 3. 추론 수행 루프 및 마이그레이션 콜백 정의
    def migration_callback(step, all_generated_ids, past_key_values):
        """
        추론 엔진에서 매 토큰 생성 시 실행되는 콜백 함수.
        백그라운드 스레드에서 stop_event가 설정되면 True를 반환하여 
        즉시 추론을 중단하고 마이그레이션 상태로 진입하게 합니다.
        """
        return stop_event.is_set()

    timestamp("Node A", f"Inference Start (Mode: {args.mode})")
    
    current_result = None
    num_batches = total_samples // batch_size
    last_batch_idx = 0
    
    for i in range(0, total_samples, batch_size):
        last_batch_idx = i
        batch_prompts = all_prompts[i : i + batch_size]
        print(f"\n[Node A] Processing batch {i//batch_size + 1}/{num_batches}...")
        
        current_result = engine.run_inference_batch(batch_prompts, max_new_tokens=200, callback=migration_callback)
        
        if stop_event.is_set():
            break
            
    timestamp("Node A", "Inference Interrupted")

    # 4. P2P 마이그레이션 수행
    if stop_event.is_set() and current_result:
        migration_start_time = time.time()
        
        # 아직 처리되지 않은 남은 프롬프트들 계산
        pending_prompts = all_prompts[last_batch_idx + batch_size:]
        
        try:
            client = TcpClient(args.dest_ip, args.port)
            
            # 선택된 모드에 따라 전송 데이터 구성
            if args.mode == "spotserve":
                # SpotServe 방식: 가중치, KV Cache, 남은 작업 모두 전송
                print(f"\n[Node A] Mode: SPOTSERVE - Migrating Weight + KV Cache + {len(pending_prompts) + batch_size} prompts...")
                migration_data = {
                    "mode": "spotserve",
                    "state_dict": engine.get_state_dict(),
                    "inference_state": current_result,
                    "pending_prompts": pending_prompts,
                    "batch_size": batch_size
                }
            else: # args.mode == "naive"
                # Naive 방식: 오직 프롬프트 전달 (중단된 배치 포함)
                print(f"\n[Node A] Mode: NAIVE - Migrating only {len(pending_prompts) + batch_size} prompts (Cold Start)...")
                migration_data = {
                    "mode": "naive",
                    "interrupted_prompts": all_prompts[last_batch_idx : last_batch_idx + batch_size],
                    "pending_prompts": pending_prompts,
                    "batch_size": batch_size
                }

            timestamp("Node A", "Migration Start")
            client.send_object(migration_data)
            timestamp("Node A", "Migration End")
            
            duration = time.time() - migration_start_time
            print(f"\n[Node A] Migration completed in {duration:.2f} seconds.")
            if duration <= 30:
                print("[Node A] SUCCESS: Migration finished within 30s grace period!")
            else:
                print("[Node A] WARNING: Migration exceeded 30s grace period. Node might have been evicted.")
            
        except Exception as e:
            print(f"[Node A] Migration failed: {e}")

    else:
        print("\n[Node A] All batches completed before migration.")

    # 자원 정리
    engine.cleanup()
    print("\n[Node A] Finished.")

if __name__ == "__main__":
    main()
