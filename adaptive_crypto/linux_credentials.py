"""Oracle Linux credential protection using a deployment-owned Fernet key."""
import os
import stat
from pathlib import Path
from .core import DataError
PREFIX=b'PYPTA-LINUX-1:'
def protect(data,decrypt=False):
    from cryptography.fernet import Fernet
    key_path=Path(os.environ['PYPTA_CREDENTIAL_KEY'])
    mode=stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o007: raise DataError('The Linux credential key must not be accessible to other users')
    cipher=Fernet(key_path.read_bytes().strip())
    if decrypt:
        if not data.startswith(PREFIX): raise DataError('Re-enter credentials on this Linux server')
        return cipher.decrypt(data[len(PREFIX):])
    return PREFIX+cipher.encrypt(data)