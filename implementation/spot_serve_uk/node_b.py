import argparse
import time
from migration_utils import TcpServer, timestamp
from opt_engine import OPTEngine
import torch

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=10051, help="Listening port")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device to use (cpu, cuda)")
    args = parser.parse_args()

    # 1. 모델 엔진 초기화 (가중치 없이 구조만 로드)
    # SpotServe의 진정한 P2P 마이그레이션을 보여주기 위해 가중치가 없는 '뼈대' 상태로 시작합니다.
    engine = OPTEngine(device=args.device, init_empty=True)

    # 2. 소켓 서버 시작 및 대기
    server = TcpServer("0.0.0.0", args.port)
    print(f"\n[Node B] Waiting for migration data on port {args.port}...")

    agent = server.accept()
    print(f"[Node B] Connection established from {agent.address}")

    # 3. 데이터 수신 (현재 엔진의 디바이스로 맵핑하며 수신)
    reception_start = time.time()
    timestamp("Node B", "Data Reception Start")
    migration_data = agent.recv_object(map_location=engine.device)
    reception_end = time.time()
    timestamp("Node B", "Data Reception End")

    mode = migration_data.get("mode", "spotserve")
    total_start_time = time.time()

    if mode == "spotserve":
        # --- [시나리오 1: SpotServe P2P 마이그레이션] ---
        print(f"\n[Node B] SCENARIO: SPOTSERVE (P2P State Injection)")
        
        # 가중치 주입 (뇌 복구)
        print("[Node B] 1. Restoring model weights via P2P injection...")
        inj_start = time.time()
        engine.load_state_dict(migration_data["state_dict"])
        injection_duration = time.time() - inj_start
        
        # KV Cache 기반 재개
        inference_state = migration_data["inference_state"]
        print(f"[Node B] 2. Resuming from step {inference_state['next_step']} using KV Cache...")
        timestamp("Node B", "Inference Resume")
        res_start = time.time()
        engine.resume_inference_batch(inference_state, max_new_tokens=200)
        resume_duration = time.time() - res_start
        
        pending_prompts = migration_data.get("pending_prompts", [])
        batch_size = migration_data.get("batch_size", 100)

    else:
        # --- [시나리오 2: Naive Cold-Start 마이그레이션] ---
        print(f"\n[Node B] SCENARIO: NAIVE (Cold Start)")
        
        # 콜드 부트: 가중치를 처음부터 다시 로드 (상당한 시간 소요)
        print("[Node B] 1. Cold Booting: Re-loading full model from disk/cache...")
        boot_start = time.time()
        engine = OPTEngine(device=args.device, init_empty=False) # 가중치 포함해서 새로 로드
        boot_duration = time.time() - boot_start
        print(f"[Node B] Cold Boot completed in {boot_duration:.2f} seconds.")

        # 처음부터 다시 계산 (중단된 배치 재시작)
        interrupted_prompts = migration_data.get("interrupted_prompts", [])
        print(f"[Node B] 2. Restarting interrupted batch (total {len(interrupted_prompts)} prompts) from step 0...")
        timestamp("Node B", "Inference Restart")
        rest_start = time.time()
        engine.run_inference_batch(interrupted_prompts, max_new_tokens=200)
        restart_duration = time.time() - rest_start
        
        pending_prompts = migration_data.get("pending_prompts", [])
        batch_size = migration_data.get("batch_size", 100)

    # 4. 남은 작업 큐 수행 (공통)
    pending_duration = 0
    if pending_prompts:
        print(f"\n[Node B] Now processing {len(pending_prompts)} remaining pending prompts...")
        p_start = time.time()
        num_batches = (len(pending_prompts) + batch_size - 1) // batch_size
        for i in range(0, len(pending_prompts), batch_size):
            current_batch = pending_prompts[i : i + batch_size]
            print(f"[Node B] Processing batch {i//batch_size + 1}/{num_batches}...")
            engine.run_inference_batch(current_batch, max_new_tokens=200)
        pending_duration = time.time() - p_start
            
    timestamp("Node B", "Inference Finish")
    total_duration = time.time() - total_start_time
    
    # 상세 지표 계산
    recv_duration = reception_end - reception_start
    
    print(f"\n{'='*60}")
    print(f"📊 [Node B] DETAILED MIGRATION REPORT (Mode: {mode.upper()})")
    print(f"{'-'*60}")
    print(f"1. Network Transfer Time   : {recv_duration:.2f} seconds")
    
    if mode == "spotserve":
        print(f"2. P2P Weight Injection    : {injection_duration:.4f} seconds (Memory Copy Only)")
        print(f"3. Resumed Batch Inference : {resume_duration:.2f} seconds (KV Cache used)")
    else:
        print(f"2. Cold Boot Overhead      : {boot_duration:.2f} seconds (Disk I/O + Init)")
        print(f"3. Restarted Batch Inference: {restart_duration:.2f} seconds (Re-computed from start)")
        
    print(f"4. Remaining Tasks Processing: {pending_duration:.2f} seconds")
    print(f"{'-'*60}")
    print(f"🚀 Total Recovery & Completion : {total_duration:.2f} seconds")
    print(f"✅ Total Prompts Completed     : {len(pending_prompts) + batch_size}")
    print(f"{'='*60}")

    # 자원 정리
    engine.cleanup()
    print("\n[Node B] All migrated tasks completed and resources cleared.")

if __name__ == "__main__":
    main()
