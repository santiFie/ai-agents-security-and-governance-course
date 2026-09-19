#!/usr/bin/env python3
"""
Lab Propuesto 4.A -- Paso 2: par RSA para firmar/verificar el JWT.

Corre una sola vez y persiste en disco (jwt_private.pem / jwt_public.pem),
reusado por server.py y client.py. No toca ningun LLM.

Correr: ./venv_jwt/bin/python3 generate_jwt_keys.py
"""
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
with open("jwt_private.pem", "wb") as f:
    f.write(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
with open("jwt_public.pem", "wb") as f:
    f.write(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
print("Par RSA para JWT generado: jwt_private.pem / jwt_public.pem")
# NOTA: en produccion este par se rota y se guarda en un KMS/Secret Manager,
# nunca en disco plano. Para el lab lo "hardcodeamos" (fijo, reusado por
# server.py y client.py) para poder verificar la firma en ambos procesos.
