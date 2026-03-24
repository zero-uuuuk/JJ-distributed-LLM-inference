import torch
import time
import socket
import sys
import os
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.utils as utils

def main():
    parser = argparse.ArgumentParser(description="Node B: Receiver")
    parser.add_argument("--port", type=int, default=10051, help="Listening port")
    parser.add_argument("--device", type=str, default="cuda", choices=["cuda", "cpu"], help="Inference device")
    args = parser.parse_args()

    config = utils.load_config()
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    
    # Deterministic generation for verification
    torch.manual_seed(config["random_seed"])

    model, tokenizer = utils.load_model_and_tokenizer(config["model_name"], device)

    print(f"\n[Node B] Awaiting KV Cache Migration on Port {args.port}...")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', args.port))
        s.listen(1)
        conn, addr = s.accept()
        with conn:
            print(f"[Node B] Connected by {addr}")
            # Receive last token
            last_token_bytes = utils.recvall(conn, 4)
            if not last_token_bytes:
                print("[Node B] Error receiving last token.")
                return
            last_token_id = int.from_bytes(last_token_bytes, byteorder='big')
            
            # Receive KV Cache
            byte_buffer = utils.recv_tensor_buffer(conn)
            if not byte_buffer:
                print("[Node B] Error receiving KV Cache buffer.")
                return
            
            print(f"[Node B] ✅ Migration Data Received. Buffer Size: {len(byte_buffer) / (1024*1024):.2f} MB")
            
            print("[Node B] Deserializing KV Cache...")
            start_deser = time.time()
            past_key_values = utils.deserialize_kv_cache(byte_buffer, device)
            print(f"  Deserialization Time: {time.time() - start_deser:.4f} sec")
            
            # Resume Generation!
            print(f"\n[Node B] Resuming Generation from token ID {last_token_id} '{tokenizer.decode(last_token_id)}'...")
            
            resume_input_ids = torch.tensor([[last_token_id]], device=device)
            generated_tokens = [last_token_id]
            
            # For verification, we capture the logits of the very first generated token after resume
            first_resume_logits = None
            
            current_input_ids = resume_input_ids
            # Generate 10 more tokens
            for step in range(10):
                with torch.no_grad():
                    outputs = model(
                        input_ids=current_input_ids,
                        past_key_values=past_key_values,
                        use_cache=True
                    )
                
                next_token_logits = outputs.logits[:, -1, :]
                if step == 0:
                    first_resume_logits = next_token_logits.clone()
                    
                next_token = torch.argmax(next_token_logits, dim=-1).unsqueeze(-1)
                
                generated_tokens.append(next_token.item())
                past_key_values = outputs.past_key_values
                current_input_ids = next_token

            print(f"[Node B] Resumed Text Generation: {tokenizer.decode(generated_tokens)}")
            
            # ========================
            # EXACT VERIFICATION
            # ========================
            print("\n[Node B] Performing Logit Verification...")
            # To verify, we compute the same thing from scratch (Control Group)
            prompt = "In the SpotServe architecture, the ability to migrate computation state is crucial for"
            control_input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)
            
            # Run up to the interruption point + 1 (to get the exact same logits)
            control_past_key_values = None
            control_current_input_ids = control_input_ids
            
            # INTERRUPT_STEP from Node A was 20. 
            # We generated 1 token per step, so after 20 steps we had 20 generated tokens.
            # Plus the initial prompt tokens.
            
            # Let's completely recalculate using generate
            # Because of manual iteration in node_a, we need to replicate the loop exactly.
            torch.manual_seed(config["random_seed"]) # Reset seed
            model, _ = utils.load_model_and_tokenizer(config["model_name"], device) # Reload to clear state
            
            control_current_input_ids = control_input_ids
            control_past_key_values = None
            control_logits = None
            
            for step in range(1, 22): # Go up to 21 to match step=0 of resumed loop
                with torch.no_grad():
                    outputs = model(
                        input_ids=control_current_input_ids,
                        past_key_values=control_past_key_values,
                        use_cache=True
                    )
                
                next_token_logits = outputs.logits[:, -1, :]
                control_logits = next_token_logits
                next_token = torch.argmax(next_token_logits, dim=-1).unsqueeze(-1)
                
                control_past_key_values = outputs.past_key_values
                control_current_input_ids = next_token
                
            utils.verify_logits(first_resume_logits, control_logits)
            
if __name__ == "__main__":
    main()
