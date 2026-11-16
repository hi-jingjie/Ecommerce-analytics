from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad, pad
import base64
from common.core.config import settings

key = settings.DS_AES_KEY.encode('utf-8')
if len(key) not in {16, 24, 32}:
    raise ValueError('DS_AES_KEY must contain exactly 16, 24, or 32 UTF-8 bytes')

def aes_encrypt(data):
    data = bytes(data,'utf-8')
    cipher = AES.new(key, AES.MODE_ECB)
    data = pad(data, AES.block_size)
    encrypt = cipher.encrypt(data)
    return base64.b64encode(encrypt)

def aes_decrypt(encrypted_data):
    encrypted_data = base64.b64decode(encrypted_data)
    cipher = AES.new(key, AES.MODE_ECB)
    text = cipher.decrypt(encrypted_data)
    decrypted_text = unpad(text, AES.block_size)
    return decrypted_text.decode('utf-8')
