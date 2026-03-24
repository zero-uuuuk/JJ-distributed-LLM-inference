import torch
import time
import sys
import os
import signal

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.utils as utils

config = utils.load_config()

# Global state to track generation
interrupted = False

def signal_handler(sig, frame):
    global interrupted
    print(f"\n[Exp 3] ⚠️ SIGUSR1 Received! Spot Instance Preemption Imminent!")
    interrupted = True

def run_threshold_algorithm(kv_cache_size_bytes, bandwidth_gbps, grace_period_sec):
    """
    Simulates a threshold algorithm to decide whether to migrate or checkpoint.
    """
    estimated_delay = utils.calculate_effective_network_delay(
        kv_cache_size_bytes, 
        bandwidth_gbps, 
        config["tcp_handshake_overhead_sec"]
    )
    
    # Add a safety margin of 20%
    safety_margin = 1.2 
    total_estimated_time = estimated_delay * safety_margin
    
    print(f"[Threshold Alg] KV Size: {kv_cache_size_bytes / (1024*1024):.2f} MB")
    print(f"[Threshold Alg] Estimated Migration Time (with margin): {total_estimated_time:.4f} sec")
    print(f"[Threshold Alg] Grace Period Remaining: {grace_period_sec} sec")
    
    if total_estimated_time < grace_period_sec:
        print("[Threshold Alg] ✅ Decision: Proceed with Live Migration")
        return True
    else:
        print("[Threshold Alg] 🛑 Decision: Abort Migration! Fallback to Checkpointing to S3.")
        return False

def main():
    global interrupted
    print(f"PID: {os.getpid()}")
    print("Send signal with: kill -SIGUSR1", os.getpid())
    
    # Register signal (Windows doesn't have SIGUSR1, we use SIGTERM or manual trigger for demo)
    if os.name != 'nt':
        signal.signal(signal.SIGUSR1, signal_handler)
    else:
        # Fallback for Windows testing
        signal.signal(signal.SIGTERM, signal_handler)
        print("Note: On Windows, use `taskkill /PID <pid>` to trigger SIGTERM.")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, tokenizer = utils.load_model_and_tokenizer(config["model_name"], device)

    prompt = "The future of cloud computing is"
    input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)
    past_key_values = None
    
    print("\n[Exp 3] Starting Long Inference... Waiting for Interruption Signal.")
    
    # Infinite loop until interrupted
    step = 0
    while not interrupted:
        with torch.no_grad():
            outputs = model(
                input_ids=input_ids,
                past_key_values=past_key_values,
                use_cache=True
            )
        
        next_token_logits = outputs.logits[:, -1, :]
        next_token = torch.argmax(next_token_logits, dim=-1).unsqueeze(-1)
        past_key_values = outputs.past_key_values
        input_ids = next_token
        
        step += 1
        sys.stdout.write(".")
        sys.stdout.flush()
        
        # Simulate time passing to allow user to send signal
        time.sleep(0.1) 
        
        # Windows automatic trigger for CI/demo if not killed
        if os.name == 'nt' and step == 30:
            print(f"\n[Demo] Auto-triggering interruption on step 30 for Windows compatibility...")
            interrupted = True

    # Grace Period logic triggered!
    grace_start = time.time()
    print("\n[Exp 3] Grace Period Countdown Started (3 seconds max) ⏱️")
    
    # 1. Serialize
    print("[Exp 3] Step 1: Serializing KV Cache...")
    start_ser = time.time()
    byte_buffer = utils.serialize_kv_cache(past_key_values)
    ser_time = time.time() - start_ser
    print(f"  Serialization Time: {ser_time:.4f} sec")
    
    # 2. Threshold Algorithm
    print("\n[Exp 3] Step 2: Running Migration Threshold Algorithm...")
    buffer_size = len(byte_buffer)
    remaining_grace = config["grace_period_sec"] - (time.time() - grace_start)
    
    can_migrate = run_threshold_algorithm(buffer_size, config["simulated_bandwidth_gbps"], remaining_grace)
    
    if can_migrate:
        # 3. Simulate Migration
        print("\n[Exp 3] Step 3: Simulating Network Transfer...")
        # Reduce the grace period remaining after serialization
        transfer_start = time.time()
        utils.simulate_network_delay(buffer_size, config["simulated_bandwidth_gbps"], config["tcp_handshake_overhead_sec"])
        transfer_time = time.time() - transfer_start
        
        total_time = (time.time() - grace_start)
        print(f"\n[Exp 3] Total Migration Pipeline Time: {total_time:.4f} sec / {config['grace_period_sec']} sec limit")
        
        if total_time <= config["grace_period_sec"]:
            print("[Exp 3] 🎉 SUCCESS: Migration completed within the Grace Period!")
        else:
            print("[Exp 3] 💥 FAILURE: Migration exceeded Grace Period. Instance Terminated mid-transfer.")
    else:
        print("\n[Exp 3] Fallback activated. Saving checkpoint locally instead of network transfer.")
        time.sleep(0.5) # Simulate local disk save
        print("[Exp 3] Checkpoint saved successfully within Grace Period.")

if __name__ == "__main__":
    main()
