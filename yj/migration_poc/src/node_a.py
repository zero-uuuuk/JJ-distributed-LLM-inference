import torch
import time
import socket
import sys
import os
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.utils as utils

def main():
    parser = argparse.ArgumentParser(description="Node A: Sender")
    parser.add_argument("--dest_ip", type=str, required=True, help="Destination IP of Node B")
    parser.add_argument("--port", type=int, default=10051, help="Destination port")
    parser.add_argument("--device", type=str, default="cuda", choices=["cuda", "cpu"], help="Inference device")
    args = parser.parse_args()

    config = utils.load_config()
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    
    # Deterministic generation
    torch.manual_seed(config["random_seed"])

    model, tokenizer = utils.load_model_and_tokenizer(config["model_name"], device)

    # Prepare input
    prompt = "In the SpotServe architecture, the ability to migrate computation state is crucial for"
    input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)
    
    # We will generate up to 250 tokens or until interrupted
    INTERRUPT_STEP = 20 # Let's say interrupt happens exactly at step 20 for determinism
    current_input_ids = input_ids
    past_key_values = None
    
    print("\n[Node A] Starting Inference...")
    start_time = time.time()
    generated_tokens = []
    
    for step in range(1, 50): # Max steps for this demo
        with torch.no_grad():
            outputs = model(
                input_ids=current_input_ids,
                past_key_values=past_key_values,
                use_cache=True
            )
            
        next_token_logits = outputs.logits[:, -1, :]
        next_token = torch.argmax(next_token_logits, dim=-1).unsqueeze(-1)
        
        generated_tokens.append(next_token.item())
        past_key_values = outputs.past_key_values
        
        # Next input is just the newest token
        current_input_ids = next_token
        
        # Simulate interruption
        if step == INTERRUPT_STEP:
            print(f"\n[Node A] 🛑 SPOT INTERRUPTION SIGNAL RECEIVED AT STEP {step}! (Simulated 20s mark)")
            print(f"[Node A] Generated text so far: {tokenizer.decode(generated_tokens)}")
            
            # Serialize KV Cache
            print("\n[Node A] Serializing KV Cache...")
            start_ser = time.time()
            byte_buffer = utils.serialize_kv_cache(past_key_values)
            print(f"  Serialization Time: {time.time() - start_ser:.4f} sec")
            
            # Additional Context to send: 
            # Node B needs to know the exact *last token* to resume generation correctly.
            # We'll pack both the last token ID and the KV cache buffer.
            # Convert last token integer to 4 bytes
            last_token_bytes = next_token.item().to_bytes(4, byteorder='big')
            
            print(f"[Node A] Connecting to Node B at {args.dest_ip}:{args.port}...")
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    # Give realistic 30s timeout
                    s.settimeout(config["migration_timeout_sec"])
                    s.connect((args.dest_ip, args.port))
                    
                    print("[Node A] Connection established. Sending Data...")
                    # Send last token
                    s.sendall(last_token_bytes)
                    # Send KV Cache
                    utils.send_tensor_buffer(s, byte_buffer)
                    
                    print("[Node A] ✅ Migration Successful! Exiting Node A gracefully within 30s limit.")
            except Exception as e:
                print(f"[Node A] ❌ Migration Failed: {e}")
            break

if __name__ == "__main__":
    main()
