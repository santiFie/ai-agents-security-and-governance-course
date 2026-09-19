#!/usr/bin/env bash
# Lab Propuesto 4.A -- Paso 1: certificados mTLS auto-firmados.
# Base: ch04-labs.md (lineas 158-177). No requiere ningun cambio para
# modelo local -- esto es infraestructura TLS pura, no toca ningun LLM.
#
# HALLAZGO REAL (corrida en vivo, 2026-07-28): los comandos openssl tal cual
# aparecen en ch04-labs.md generan una CA SIN el extension keyUsage. Contra
# OpenSSL moderno (3.x, el que trae Python 3.14 de esta maquina) el handshake
# TLS del cliente falla con:
#   [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: CA cert does
#   not include key usage extension
# -- la validacion de cadena moderna exige que una CA declare explicitamente
# keyCertSign (puede firmar certificados) via keyUsage, y el libro no lo
# agrega. Fix: `-addext` en el `openssl req -x509` de la CA (basicConstraints
# + keyUsage) y `-addext` con extendedKeyUsage en los CSR de servidor/cliente
# para que serverAuth/clientAuth tambien queden declarados explicitamente.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs && cd certs

# CA propia del lab -- CA:TRUE + keyCertSign explicitos (ver HALLAZGO REAL arriba)
openssl genrsa -out ca.key 2048
openssl req -x509 -new -nodes -key ca.key -sha256 -days 365 -out ca.crt -subj "/CN=Lab4A-CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"

# Certificado del servidor (MCP Server), firmado por la CA
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=localhost" \
    -addext "extendedKeyUsage=serverAuth" -addext "subjectAltName=DNS:localhost"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256 \
    -copy_extensions copyall

# Certificado del cliente (agente), firmado por la MISMA CA -- esto es lo que
# el servidor exige para completar el handshake mTLS (ssl_cert_reqs=CERT_REQUIRED)
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr -subj "/CN=logistics-agent" \
    -addext "extendedKeyUsage=clientAuth"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out client.crt -days 365 -sha256 \
    -copy_extensions copyall

cd ..
echo "Certificados generados en certs/: ca.crt/ca.key, server.crt/server.key, client.crt/client.key"
