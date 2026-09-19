# Lab 4.A — MCP Server con mTLS + JWT + Role-Check

## Qué es este lab

Es una prueba de defensa en profundidad para un MCP Server: tres capas
independientes que tienen que pasar todas para que una tool se ejecute.
Capa 1, mTLS: el servidor exige un certificado de cliente válido, firmado
por la misma CA del lab (`ssl_cert_reqs=CERT_REQUIRED`) — sin eso, el
handshake TLS ni siquiera se completa. Capa 2, JWT: el cliente manda un
token RS256 firmado con una clave privada que solo el emisor legítimo
tiene; el servidor lo valida contra la clave pública correspondiente. Capa
3, role-check: aunque el JWT sea válido, el `role` que declara se compara
contra un `ROLE_TOOL_ALLOWLIST` explícito antes de ejecutar la tool
pedida — un `logistics-agent` con JWT perfectamente válido no puede llamar
`get_budget_summary`, punto. Es la misma lógica de enforcement inline que
Lab 3.1 (OPA) aplica con un motor de políticas externo, acá resuelta con
una allowlist en el propio servidor.

## Las piezas que corren

- `gen_certs.sh`: genera la CA del lab y los certificados de
  servidor/cliente con `openssl`.
- `generate_jwt_keys.py`: genera el par RSA que firma y verifica los JWTs
  (persistido en disco, solo para el lab — en producción esto va a un
  KMS).
- `server.py`: el MCP Server. Sirve HTTPS con mTLS obligatorio, valida el
  JWT del header `Authorization`, y aplica el role-check antes de invocar
  `execute_tool`.
- `client.py`: el cliente (agente). Arma un `SSLContext` con el
  certificado de cliente cargado, firma un JWT con el rol que le pasan, y
  llama `/tools/call`.
- `agent_step5_ollama.py`: el paso opcional — un `Agent` ADK real (no un
  script HTTP a mano) que consulta el mismo servidor a través de tools que
  envuelven el cliente mTLS+JWT.

## El flujo

1. `gen_certs.sh` + `generate_jwt_keys.py` (una sola vez).
2. `server.py` arriba en `:8443`.
3. `client.py` hace tres llamadas: `logistics-agent` pidiendo inventario
   (autorizado, 200), `logistics-agent` pidiendo presupuesto (JWT válido
   pero rol no autorizado, 403), `finance-agent` pidiendo presupuesto
   (autorizado, 200). Un cuarto chequeo manual con `curl` sin certificado
   de cliente confirma que mTLS no es opcional: el handshake falla antes
   de llegar siquiera al JWT.
4. `agent_step5_ollama.py` (opcional): el mismo flujo, pero disparado por
   un LLM en vez de por llamadas directas en `__main__`.

## Por qué importa la verificación

Los comandos `openssl` más directos generan una CA **sin la extensión
`keyUsage`**. Contra OpenSSL moderno, el handshake del cliente falla con
`certificate verify failed: CA cert does not include key usage extension`
— la validación de cadena actual exige que una CA declare explícitamente
`keyCertSign` para poder firmar certificados. Sin correr el lab de punta a
punta, esto queda invisible: el código "se ve bien", compila, y solo se
rompe al intentar el handshake real. El fix (agregar `-addext
"keyUsage=critical,keyCertSign,cRLSign"` a la generación de la CA, más
`extendedKeyUsage` en servidor/cliente) ya está aplicado en `gen_certs.sh`.

Segundo punto de diseño: `server.py` es un REST API liso (`POST
/tools/call`), no un servidor MCP/SSE compliant — por eso `agent_step5_*`
no usa `MCPToolset(SseConnectionParams(...))` (que negociaría un handshake
MCP que este servidor no habla), sino una tool que envuelve el cliente
mTLS+JWT ya verificado.

## Resultado esperado

`get_inventory_count` con rol `logistics-agent`: HTTP 200.
`get_budget_summary` con rol `logistics-agent`: HTTP 403 (bloqueado por el
role-check, JWT válido igual). `get_budget_summary` con rol
`finance-agent`: HTTP 200. `curl` sin certificado de cliente: falla el
handshake TLS antes de llegar a la aplicación.

Con `agent_step5_ollama.py`, el `Agent` ADK invoca correctamente
`get_inventory_count`/`check_stock_location` a través del pipeline
completo y devuelve un reporte con el stock consultado.

## Requisitos de infraestructura

Un venv propio (`venv_jwt/`), creado con `--system-site-packages` para
heredar `google-adk`/`fastapi`/`httpx`/`cryptography` del sistema y
agregar `python-jose[cryptography]` — necesario en sistemas donde el
Python global es "externally managed" (PEP 668).

## Hallazgos técnicos

El rol del agente (`logistics-agent`) está fijado por código en
`agent_step5_ollama.py`, no expuesto como parámetro libre al LLM — es una
decisión de seguridad, no algo que el usuario deba poder elegir.

## Backend Gemini (opcional)

El agente puede correr contra Gemini en Vertex AI en vez de Ollama local
en el Paso 5, sin tocar mTLS/JWT/role-check — solo cambia el backend del
modelo. Ver `ch04-labA-gcp/` para la variante equivalente.
