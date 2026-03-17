import socket
import struct
import io
import torch
import pickle
import sys
import time
from datetime import datetime

def timestamp(name, stage):
    """
    디버깅 및 지연 시간 측정을 위해 [HH:MM:SS.mmm] 형식으로 타임스탬프를 출력합니다.
    - name: 노드 이름 (Node A, Node B 등)
    - stage: 현재 진행 중인 마이그레이션 단계
    """
    now = datetime.now()
    time_str = now.strftime("%H:%M:%S")
    millis = now.microsecond // 1000
    print(f'[{time_str}.{millis:03d}] {name:7s} | {stage}', file=sys.stderr)

class TcpAgent:
    """
    SpotServe Reference: elastic-switch/util/util.py
    노드 간 P2P 통신을 관리하며 모델 가중치 및 KV Cache 등의 대규모 객체를 전송합니다.
    """
    def __init__(self, conn, address=None, blocking=True):
        """
        - conn: 확립된 소켓 연결 객체
        - address: 상대방 노드의 주소 정보
        - blocking: 블로킹/넌블로킹 모드 설정
        """
        self.conn = conn
        self.address = address
        if self.conn:
            self.conn.setblocking(blocking)

    def __del__(self):
        """인스턴스 소멸 시 안전하게 소켓 연결을 닫아 자원 누수를 방지합니다."""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()

    def send(self, msg):
        """바이트 데이터를 상대방 노드로 전송합니다."""
        self.conn.sendall(msg)

    def recv(self, msg_len):
        """
        데이터 유실 방지를 위한 루프 기반 수신 로직.
        네트워크 상태에 따라 패킷이 쪼개져 오더라도 msg_len만큼 확실히 수신할 때까지 반복합니다.
        """
        msg = b''
        while len(msg) < msg_len:
            chunk = self.conn.recv(msg_len - len(msg))
            if not chunk:
                raise Exception('Connection closed by remote end')
            msg += chunk
        return msg

    def send_string(self, s):
        """문자열 형태의 메타데이터(파일명, 시나리오 ID 등)를 전송합니다."""
        data = s.encode('utf-8')
        l = len(data)
        # 4바이트 정수형(I)으로 데이터의 길이를 먼저 전송하여 수신 측이 크기를 알게 함
        self.conn.sendall(struct.pack('I', l))
        self.conn.sendall(data)

    def recv_string(self):
        """길이 정보를 먼저 읽은 뒤 실 데이터를 수신하여 문자열로 변환합니다."""
        l_data = self.recv(4)
        l, = struct.unpack('I', l_data)
        data = self.recv(l)
        return data.decode('utf-8')

    # SpotServe Reference: ParamsClient/src/client/TensorStorage.cc
    # TensorStorage의 SEND/RECV 로직을 Python 환경에 맞춰 포팅.
    # PyTorch 텐서(모델 가중치, KV Cache)를 직렬화하여 P2P로 전송하는 핵심 메서드.
    def send_object(self, obj):
        """
        Python 객체 및 PyTorch 텐서를 메모리 버퍼에서 직렬화하여 전송합니다.
        - torch.save: 객체 상태를 바이트 시퀀스로 변환
        - struct.pack('Q', l): 8바이트로 데이터 전체 크기를 선행 전송
        """
        buffer = io.BytesIO()
        torch.save(obj, buffer)
        data = buffer.getvalue()
        l = len(data)
        self.conn.sendall(struct.pack('Q', l)) 
        self.conn.sendall(data)

    def recv_object(self, map_location=None):
        """
        전송받은 바이트 데이터를 역직렬화하여 객체를 복원합니다.
        - map_location: 노드 B의 장치 특성(CPU/GPU)에 맞춰 텐서를 자동 배치
        """
        l_data = self.recv(8)
        l, = struct.unpack('Q', l_data)
        data = self.recv(l)
        buffer = io.BytesIO(data)
        return torch.load(buffer, map_location=map_location, weights_only=False)

class TcpServer:
    """
    SpotServe Reference: elastic-switch/util/util.py
    특정 주소와 포트에서 클라이언트의 접속을 대기하는 서버 클래스입니다.
    데이터를 직접 주고받지 않고, 연결이 확립되면 실제 통신을 담당할 TcpAgent를 생성합니다.
    """
    def __init__(self, address, port, blocking=True):
        self.address = address
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR: 서버 재시작 시 포트 점유 에러를 방지
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.address, self.port))
        self.sock.listen(1) # 한 번에 하나의 마이그레이션 요청만 처리
        self.sock.setblocking(blocking)

    def accept(self):
        """클라이언트 접속을 수락하고, 해당 연결을 전담할 TcpAgent 인스턴스를 반환합니다."""
        conn, address = self.sock.accept()
        return TcpAgent(conn, address)

    def __del__(self):
        """서버 종료 시 리스닝 소켓을 닫습니다."""
        if hasattr(self, 'sock'):
            self.sock.close()

class TcpClient(TcpAgent):
    """
    상대방 노드(Server)에 접속을 시도하는 클라이언트 클래스입니다.
    TcpAgent를 상속받아, 연결 성공 즉시 데이터를 전송할 수 있는 기능을 갖춥니다.
    """
    def __init__(self, address, port):
        # 새로운 소켓을 생성하여 서버에 접속
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.connect((address, port))
        # 부모 클래스(TcpAgent)를 초기화하여 send/recv 기능을 활성화
        super().__init__(conn, (address, port))
