# Lab 4.B — CAEP Simulation: Revocación en Tiempo Real

## Qué es este lab

Es una prueba de contención rápida: si un agente comprometido (o
simplemente comportándose fuera de su rol) puede ser desconectado en
segundos en vez de en horas. CAEP (Continuous Access Evaluation Protocol,
parte del Shared Signals Framework) es el estándar que permite esto — en
vez de que un token siga siendo válido hasta que expira por las suyas
(minutos u horas después), un Identity Provider puede emitir un evento de
"sesión revocada" en caliente, y cualquier Resource Server suscripto a ese
canal corta el acceso en la siguiente llamada. Este lab simula ambos lados
(IdP y Resource Server) en un solo proceso, comunicados por un
`asyncio.Queue` que hace de stand-in del canal real (en producción sería
un stream HTTP/websockets).

## Las piezas que corren

- `ACTIVE_SESSIONS` / `ACTION_LOG`: el estado compartido — qué agentes
  tienen sesión activa y qué tools invocaron.
- `idp_security_event_stream()`: el IdP simulado. Corre una heurística
  simple (`detect_anomaly_from_logs`) que mira el log de acciones: si un
  `logistics-agent` invocó `get_budget_summary` (una tool fuera de su rol)
  tres veces, lo trata como escalación de privilegio y emite un evento
  CAEP al bus.
- `resource_server_caep_listener()`: el Resource Server. Escucha el bus y,
  al recibir un evento `session_revoked`, marca la sesión como revocada.
- `simulate_agent_traffic()`: genera el tráfico — una llamada legítima,
  tres llamadas fuera de rol (el patrón anómalo), y un intento posterior
  que debería ser rechazado.

## El flujo

1. El agente llama `get_inventory_count` (normal, se registra en el log).
2. El agente llama `get_budget_summary` tres veces seguidas (el patrón
   anómalo).
3. El IdP, corriendo en paralelo, detecta el patrón y emite el evento CAEP
   al bus.
4. El Resource Server consume el evento y marca la sesión como revocada.
5. El agente intenta una cuarta llamada (una tool legítima,
   `get_inventory_count`) — pero como su sesión ya está revocada, la
   llamada se rechaza antes de ejecutarse.

## Por qué importa la verificación

Este lab no toca ningún LLM en ningún momento — es `asyncio` y
`dataclasses` puros, sin dependencias externas. Eso lo vuelve el más
simple de verificar: no hay ambigüedad sobre si "el modelo alucinó" un
resultado, porque no hay modelo. Un `assert` explícito al final de
`main()` confirma que `session.revoked is True` tras la secuencia, para no
depender de leer la consola a ojo.

## Resultado esperado

La secuencia de prints muestra: la llamada legítima ejecutándose, las tres
llamadas anómalas ejecutándose (el IdP todavía no reaccionó), el evento
CAEP emitido con el motivo exacto de la anomalía, la sesión revocada del
lado del Resource Server, y el intento final rechazado con `❌ RECHAZADO:
sesión revocada por CAEP` — pese a ser una tool legítima que el agente sí
tenía permiso de usar antes de la revocación. El script termina con
`[verificación] OK`.

## Requisitos de infraestructura

Ninguno — corre local con la librería estándar de Python (`python3
lab_4b_caep.py`).

## Backend Gemini (opcional)

No aplica — este lab no usa ningún LLM, el código es idéntico en
`ch04-labB-gcp/`.
