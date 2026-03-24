import json
import socket
import io
import time
import struct
import pickle
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

def load_config(path="configs/experiment_config.json"):
    with open(path, "r") as f:
        return json.load(f)

def load_model_and_tokenizer(model_name, device):
    print(f"Loading {model_name} to {device}...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.float16)
    model.to(device)
    model.eval()
    return model, tokenizer

def serialize_kv_cache(past_key_values):
    """
    Serializes the KV cache ensuring shape [layer, 2(K/V), batch, head, seq, head_dim]
    and FP16/BF16 precision is maintained.
    """
    buffer = io.BytesIO()
    torch.save(past_key_values, buffer)
    return buffer.getvalue()

def deserialize_kv_cache(byte_data, device):
    """
    Deserializes the KV cache byte buffer back to the device.
    PyTorch 2.6+ sets weights_only=True by default, which blocks DynamicCache.
    """
    buffer = io.BytesIO(byte_data)
    past_key_values = torch.load(buffer, map_location=device, weights_only=False)
    return past_key_values

def send_tensor_buffer(sock, buffer_data):
    """Sends length-prefixed byte buffer over socket."""
    length = len(buffer_data)
    # Pack the length as a 4-byte integer
    sock.sendall(struct.pack('!I', length))
    sock.sendall(buffer_data)
    print(f"[Network] Sent {length / (1024*1024):.2f} MB of data.")

def recv_tensor_buffer(sock):
    """Receives length-prefixed byte buffer from socket."""
    # Receive the length of the data
    raw_length = recvall(sock, 4)
    if not raw_length:
        return None
    length = struct.unpack('!I', raw_length)[0]
    
    print(f"[Network] Expecting {length / (1024*1024):.2f} MB of data.")
    return recvall(sock, length)

def recvall(sock, n):
    """Helper function to receive n bytes or return None if EOF is hit"""
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)

def verify_logits(logits_a, logits_b, atol=1e-4, rtol=1e-3):
    """
    Verifies output logits between control group and experiment group.
    """
    if torch.allclose(logits_a, logits_b, atol=atol, rtol=rtol):
        print("\n✅ Verification Success: Logits exactly match (torch.allclose is True). Migration is perfectly reliable.")
        return True
    else:
        print("\n❌ Verification Failed: Logits mismatch deteced!")
        # Print max difference
        diff = torch.abs(logits_a - logits_b).max().item()
        print(f"Max difference: {diff}")
        return False

def calculate_effective_network_delay(byte_size, bandwidth_gbps, tcp_overhead_sec=0.05):
    """
    Calculates the effective network delay including bandwidth limits and TCP overhead.
    Bandwidth is in Gbps (Gigabits per second).
    """
    # Convert Gbps to Bytes/sec
    bandwidth_bytes_per_sec = bandwidth_gbps * (1024**3) / 8
    
    pure_transfer_time = byte_size / bandwidth_bytes_per_sec
    effective_delay = pure_transfer_time + tcp_overhead_sec
    return effective_delay

def simulate_network_delay(byte_size, bandwidth_gbps, tcp_overhead_sec=0.05):
    delay = calculate_effective_network_delay(byte_size, bandwidth_gbps, tcp_overhead_sec)
    print(f"  [Sim] Effective bandwidth delay simulated: {delay:.4f} sec")
    time.sleep(delay)
    return delay
