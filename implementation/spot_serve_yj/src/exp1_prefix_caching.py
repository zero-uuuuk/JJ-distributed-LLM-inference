import torch
import time
import sys
import os

# Add parent dir to path to import utils
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.utils as utils

def forward_with_time(model, input_ids, past_key_values=None):
    start_time = time.perf_counter()
    
    with torch.no_grad():
        outputs = model(
            input_ids=input_ids,
            past_key_values=past_key_values,
            use_cache=True
        )
        
    end_time = time.perf_counter()
    duration = end_time - start_time
    
    return outputs, duration

def main():
    config = utils.load_config()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, tokenizer = utils.load_model_and_tokenizer(config["model_name"], device)

    print("\n[Experiment 1] Prefix Caching vs Recomputation\n")
    
    # We must ensure the system prompt length + user prompt length doesn't exceed the model's max_position_embeddings
    max_supported = model.config.max_position_embeddings
    reserved_tokens = 50 
    safe_sys_length = min(config.get("system_prompt_length", 1500), max_supported - reserved_tokens)
    
    # Generate a long string, then strictly slice the tokenized tensor
    long_text = "You are a helpful assistant. " * (safe_sys_length // 2 + 50)
    sys_tokens_full = tokenizer(long_text, return_tensors="pt").input_ids
    sys_tokens = sys_tokens_full[:, :safe_sys_length].to(device)
    
    user_prompts = [f"User prompt number {i}: Hello!" for i in range(5)] # Test with 5 prompts
    
    print(f"Model Max Length: {max_supported}")
    print(f"System Prompt Length: {sys_tokens.shape[1]} tokens")

    # --- Baseline (Recomputation) ---
    print("\n--- Baseline: Caching OFF (Recomputation) ---")
    baseline_times = []
    
    for i, user_p in enumerate(user_prompts):
        user_tokens = tokenizer(user_p, return_tensors="pt").input_ids.to(device)
        # Concatenate system prompt AND user prompt every time
        full_input_ids = torch.cat([sys_tokens, user_tokens], dim=-1)
        
        _, duration = forward_with_time(model, full_input_ids)
        baseline_times.append(duration)
        if i == 0:
            print(f"  Prompt {i} TTFT: {duration:.4f} sec (First token includes warm-up)")
        else:
            print(f"  Prompt {i} TTFT: {duration:.4f} sec")
            
    avg_baseline = sum(baseline_times[1:]) / len(baseline_times[1:])
    print(f">> Avg TTFT (excl. warmup): {avg_baseline:.4f} sec")

    # --- Prefix Caching ON ---
    print("\n--- Experiment: Caching ON (State Reuse) ---")
    caching_times = []
    
    # Precompute System Prompt KV Cache
    print("Pre-computing System Prompt KV Cache...")
    with torch.no_grad():
        sys_outputs = model(input_ids=sys_tokens, use_cache=True)
        saved_past_key_values = sys_outputs.past_key_values
        
    for i, user_p in enumerate(user_prompts):
        user_tokens = tokenizer(user_p, return_tensors="pt").input_ids.to(device)
        
        # Pass ONLY user prompt tokens + the saved_past_key_values
        _, duration = forward_with_time(model, input_ids=user_tokens, past_key_values=saved_past_key_values)
        caching_times.append(duration)
        print(f"  Prompt {i} TTFT: {duration:.4f} sec")

    avg_caching = sum(caching_times[1:]) / len(caching_times[1:])
    print(f">> Avg TTFT (excl. warmup): {avg_caching:.4f} sec")
    
    # --- Conclusion ---
    if avg_baseline > 0 and avg_caching > 0:
        speedup = avg_baseline / avg_caching
        print(f"\n[Result] Prefix Caching is {speedup:.2f}x faster than Recomputation!")

if __name__ == "__main__":
    main()
