import torch
import time
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.utils as utils

def profile_recomputation(model, tokenizer, device, context_length):
    """Measures the time it takes to process `context_length` tokens from scratch."""
    prompt = "The " * context_length
    input_ids = tokenizer(prompt, return_tensors="pt").input_ids[:, :context_length].to(device)
    
    start_time = time.time()
    with torch.no_grad():
        _ = model(input_ids=input_ids, use_cache=True)
        
    # Just need the forward pass time for the prompt processing
    return time.time() - start_time

def main():
    config = utils.load_config()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, tokenizer = utils.load_model_and_tokenizer(config["model_name"], device)

    # We will test over a log scale of context lengths
    test_lengths = [50, 150, 300, 500, 1000]
    bandwidth_gbps = config["simulated_bandwidth_gbps"]
    tcp_overhead = config["tcp_handshake_overhead_sec"]
    
    # Check max length
    max_supported = model.config.max_position_embeddings
    
    print(f"\n[Exp 4] Profiling Trade-off: Communication vs Recomputation")
    print(f"  Model: {config['model_name']} | Max Length: {max_supported}")
    print(f"  Simulated Bandwidth: {bandwidth_gbps} Gbps")
    print(f"  TCP Overhead: {tcp_overhead} sec\n")

    print(f"{'Context Len':<15} | {'KV Size (MB)':<15} | {'Recompute (s)':<15} | {'Migrate (s)':<15} | {'Winner'}")
    print("-" * 80)
    
    for length in test_lengths:
        if length > max_supported:
            break
            
        prompt = "The " * length
        input_ids = tokenizer(prompt, return_tensors="pt").input_ids[:, :length].to(device)
        
        # 1. Measure Recomputation
        # Run once to warm up GPU
        _ = profile_recomputation(model, tokenizer, device, 10)
        recomp_time = profile_recomputation(model, tokenizer, device, length)
        
        # 2. Get KV Cache size by running inference and extracting it
        with torch.no_grad():
            outputs = model(input_ids=input_ids, use_cache=True)
            past_key_values = outputs.past_key_values
            
        start_ser = time.time()
        byte_buffer = utils.serialize_kv_cache(past_key_values)
        ser_time = time.time() - start_ser
        
        buffer_size_mb = len(byte_buffer) / (1024 * 1024)
        
        # 3. Calculate Effective Network Delay
        net_delay = utils.calculate_effective_network_delay(len(byte_buffer), bandwidth_gbps, tcp_overhead)
        
        # Total Migration Time = Serialization + Network (assuming Node B deserializes concurrently in practice, or add deserialization)
        # Let's add basic deserialization penalty equivalent to serialization penalty
        total_migrate_time = ser_time + net_delay + ser_time 
        
        # Winner
        winner = "Migrate 🏃" if total_migrate_time < recomp_time else "Recompute 🧠"
        
        print(f"{length:<15} | {buffer_size_mb:<15.2f} | {recomp_time:<15.4f} | {total_migrate_time:<15.4f} | {winner}")

    print("\n[Conclusion]")
    print("If Recompute > Migrate time, SpotServe's KV Migration strategy saves overall inference latency.")
    print("For very small models (like 125m) or very poor bandwidth, Recomputation might win at short lengths.")

if __name__ == "__main__":
    main()
