# Lab 4.C — Cloud Pub/Sub: Comunicación Asíncrona entre Agentes

## Qué es este lab

Es una prueba de desacople: un agente orquestador publica una tarea en un
bus de mensajes y sigue con lo suyo, sin esperar respuesta síncrona; un
agente especializado, suscripto a ese bus, la recibe y procesa cuando
puede. El lab está diseñado "local-first, GCP-opcional": si hay
credenciales de Google Cloud disponibles y utilizables, usa Cloud Pub/Sub
real; si no, cae automáticamente a una `queue.Queue()` de la librería
estándar. La lógica de negocio (`delegate_to_specialist`,
`process_message`) es idéntica corra donde corra — el backend de
mensajería es un detalle de infraestructura, no algo que el resto del
código necesite saber.

## Las piezas que corren

- El probe de Cloud Pub/Sub al inicio del archivo, que decide
  `_USE_CLOUD_PUBSUB`.
- `LocalMessage`: una clase que imita la interfaz mínima de un mensaje
  real de Pub/Sub (`.data`, `.ack()`, `.nack()`) para que
  `process_message()` funcione igual sin importar el backend.
- `delegate_to_specialist()`: el publisher — arma el mensaje (con
  `goal_id`, `target_agent`, `task`) y lo publica.
- `logi_agent`: el agente especializado (ADK + Ollama), con una única tool
  `get_inventory_count` protegida por un role-check simplificado, portado
  del mismo patrón de Lab 4.A.
- `process_message()` / `start_subscriber_loop()`: el subscriber —
  desempaqueta el mensaje, si el `target_agent` coincide con este agente
  lo procesa (`ack()`), si no lo devuelve a la cola (`nack()`).

## El flujo

1. Se publica una tarea con un `goal_id` para `logistics-agent`.
2. El subscriber la saca de la cola, ejecuta al agente (que llama
   `get_inventory_count`), y confirma (`ack`).
3. El resultado se "publica" de vuelta (impreso en consola, citando el
   `goal_id`).
4. Una segunda tarea dirigida a `finance-agent` (que este proceso no
   maneja) se rechaza con `nack()` y vuelve a encolarse — así se comporta
   un consumidor que no es el destinatario correcto de un mensaje.

## Por qué importa la verificación

Este lab reutiliza el patrón de agente de Lab 4.A, pero encadenar 4.C a
que el servidor mTLS+JWT de 4.A esté corriendo en `:8443` acoplaría un lab
de *mensajería* a la infraestructura de *otro* lab — si alguien corre 4.C
solo, se rompería por una razón ajena al punto pedagógico de este lab. Se
resuelve reimplementando el mismo role-check como función Python pura,
in-process.

Un `try/except` "simple" alrededor de `publisher.get_topic()` no alcanza
para detectar si Cloud Pub/Sub está disponible: con credenciales ADC
*presentes pero vencidas*, esa llamada no lanza una excepción rápida — se
queda **colgada** intentando reautenticar. Un `try/except` no protege
contra un hang, solo contra una excepción lanzada. El fix acota el probe
con un thread daemon + timeout de 3 segundos; si no resuelve a tiempo, se
asume que Cloud Pub/Sub no está disponible y se cae al bus local, que es
exactamente el comportamiento "local-first" que el lab promete.

## Resultado esperado

`[bus] Backend de mensajeria: local (queue.Queue)` si no hay credenciales
de Cloud Pub/Sub utilizables (comportamiento correcto, no un bug). La
tarea de `logistics-agent` se procesa y confirma con su `goal_id`. La
tarea de `finance-agent` se rechaza y reencola.

## Requisitos de infraestructura

Ninguno obligatorio — el bus local (`queue.Queue`) es el backend por
defecto. Cloud Pub/Sub real es opcional, y requiere credenciales GCP
utilizables.

## Backend Gemini (opcional)

`logi_agent` puede correr contra Gemini en Vertex AI en vez de Ollama
local, sin tocar la lógica de mensajería. Ver `ch04-labC-gcp/` para la
variante equivalente.
