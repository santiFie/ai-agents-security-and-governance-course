#!/usr/bin/env python3
"""
Lab 3.3 — GCP IAM para Agentes: Secretless Architecture (version con
modelo local via Ollama).

Patron secretless: nunca hardcodear credenciales, siempre obtenerlas de una
fuente externa en runtime, con Secret Manager como backend opcional y un
vault JSON local versionado como fallback -100% ejecutable sin cuenta GCP.

El agente usa qwen3.5:9b corriendo localmente en Ollama via el wrapper
LiteLlm de ADK (num_ctx=8192, temperature=0.2, reasoning_effort="none").

Incluye un circuit breaker para el fallback a GCP Secret Manager. Nota de
diseño, para quien quiera entender por qué existe: si hay un archivo de ADC
(`~/.config/gcloud/application_default_credentials.json`) pero las
credenciales están vencidas, `SecretManagerServiceClient()` se construye
sin error (no hace ninguna llamada de red en el constructor), pero la
primera llamada real (`access_secret_version`) intenta refrescar el token y
se cuelga -y no alcanza con pasarle `timeout=` al propio RPC, porque el
colgado ocurre en el paso de refresh de credenciales de `google-auth`,
antes del RPC en sí. Por eso el circuit breaker acota el intento completo
(constructor + refresh + RPC) desde afuera, con un hilo, en vez de confiar
en el `timeout=` del RPC: si el intento no vuelve dentro de
`_GCP_TIMEOUT_SECONDS`, se abre el circuito y todas las llamadas siguientes
de la sesión caen directo al vault local, sin reintentar GCP. El hilo que
acota el intento es un `threading.Thread(daemon=True)` y no un
`ThreadPoolExecutor`: un executor registra un hook de `atexit` que espera a
que todos sus hilos terminen antes de dejar salir al intérprete, y un hilo
bloqueado contra GCP nunca termina -un hilo daemon no tiene ese problema.

Con ADC vigentes (caso feliz), el breaker nunca se activa y el
comportamiento es transparente: usa Secret Manager sin fricción.

Requiere: google-adk instalado (ya en el entorno). `google-cloud-
secretmanager` es opcional -si no está instalado, o si no hay ADC, el lab
sigue siendo 100% ejecutable con el vault JSON local.

Modo de verificación sin tocar Ollama (núcleo determinista: vault local,
rotación, revocación, y el circuit breaker de Secret Manager):
    python3 secretless_lab33.py --selftest

Modo con el agente real (dispara llamadas a Ollama):
    python3 secretless_lab33.py
"""
import json
import os
import sys
import threading
import time

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
import google.genai.types as types

LOCAL_VAULT_PATH = "/tmp/local_secrets_vault_lab33.json"
_GCP_TIMEOUT_SECONDS = 5  # acota el intento COMPLETO (constructor+refresh+RPC)


# ── Patron secretless: backend local por defecto, GCP Secret Manager opcional ─
class SecretlessConfig:
    """Gestiona credenciales sin hardcodeo. Usa GCP Secret Manager si hay
    credenciales disponibles; si no (o si el circuit breaker se activa), cae
    a un vault JSON local versionado (simula rotacion/revocacion de
    secretos sin depender de GCP)."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._cache = {}  # cache con TTL para evitar exceso de requests
        self.client = None
        self._circuit_open = False  # True = GCP deshabilitado para el resto de la sesion
        try:
            from google.cloud import secretmanager
            self.client = secretmanager.SecretManagerServiceClient()
        except Exception:
            # Sin ADC/credenciales disponibles (o libreria no instalada): el
            # lab sigue siendo 100% ejecutable local, con el vault JSON.
            self.client = None

    # ── Backend local: vault JSON con versiones (lista + indice "activo") ──
    def _load_vault(self) -> dict:
        if not os.path.exists(LOCAL_VAULT_PATH):
            return {}
        with open(LOCAL_VAULT_PATH, "r") as f:
            return json.load(f)

    def _save_vault(self, vault: dict) -> None:
        with open(LOCAL_VAULT_PATH, "w") as f:
            json.dump(vault, f, indent=2)

    def _get_secret_local(self, secret_name: str) -> str:
        vault = self._load_vault()
        entry = vault.get(secret_name)
        if entry is None:
            # Primera ejecucion: creamos un secreto dummy para que el lab
            # sea ejecutable out-of-the-box.
            entry = {"versions": ["dummy-local-secret-v1"], "active": 0, "disabled": []}
            vault[secret_name] = entry
            self._save_vault(vault)
        active_idx = entry["active"]
        return entry["versions"][active_idx]

    def _get_secret_gcp(self, secret_name: str) -> str:
        """La llamada real a Secret Manager -- se ejecuta SIEMPRE dentro del
        ThreadPoolExecutor del circuit breaker, nunca directo en el hilo
        principal (ver get_secret)."""
        name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
        response = self.client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")

    def get_secret(self, secret_name: str) -> str:
        """Obtiene la version mas reciente (activa) de un secreto.

        Circuit breaker: si `self.client` existe (libreria instalada +
        constructor no fallo) pero el circuito ya esta abierto (un intento
        previo colgo/fallo), no se vuelve a intentar GCP en esta sesion --
        se cae directo al vault local. Si el circuito esta cerrado, se
        intenta GCP en un hilo DAEMON con timeout acotado por fuera; si no
        vuelve a tiempo, se abre el circuito y se cae al vault local para
        ESTA llamada y todas las siguientes.

        Por que un hilo daemon crudo y no `ThreadPoolExecutor`: el executor
        registra un hook de atexit que espera a que sus hilos terminen antes
        de dejar salir al proceso -si el hilo queda colgado contra GCP, eso
        cuelga el proceso entero al final, aunque toda la logica de negocio
        ya haya terminado. Un hilo daemon no tiene ese problema: el
        interprete lo mata sin esperar.
        """
        if self.client is None or self._circuit_open:
            return self._get_secret_local(secret_name)

        result: dict = {}

        def _worker():
            try:
                result["value"] = self._get_secret_gcp(secret_name)
            except Exception as e:
                result["error"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(_GCP_TIMEOUT_SECONDS)

        if t.is_alive() or "error" in result:
            reason = (
                f"no respondio en {_GCP_TIMEOUT_SECONDS}s (colgado)"
                if t.is_alive()
                else f"fallo: {type(result['error']).__name__}: {result['error']}"
            )
            print(f"[circuit breaker] Secret Manager {reason}")
            print("[circuit breaker] Circuito ABIERTO -- el resto de la sesion "
                  "usa el vault local, sin reintentar GCP.")
            self._circuit_open = True
            return self._get_secret_local(secret_name)

        return result["value"]

    def rotate_secret(self, secret_name: str, new_value: str) -> None:
        """Simula la rotacion: agrega una version nueva y la marca como activa."""
        if self.client is not None and not self._circuit_open:
            raise NotImplementedError(
                "Rotacion real: usar `gcloud secrets versions add` (ver Ejercicio 3)."
            )
        vault = self._load_vault()
        entry = vault.setdefault(secret_name, {"versions": [], "active": -1, "disabled": []})
        entry["versions"].append(new_value)
        entry["active"] = len(entry["versions"]) - 1
        vault[secret_name] = entry
        self._save_vault(vault)

    def disable_version(self, secret_name: str, version: int) -> None:
        """Simula revocar una version especifica (indice 0-based)."""
        if self.client is not None and not self._circuit_open:
            raise NotImplementedError(
                "Revocacion real: usar `gcloud secrets versions disable` (ver Ejercicio 4)."
            )
        vault = self._load_vault()
        entry = vault.get(secret_name)
        if entry is None:
            raise KeyError(f"Secreto '{secret_name}' no existe en el vault local")
        if version not in entry["disabled"]:
            entry["disabled"].append(version)
        # Si la version activa fue revocada, cae a la version habilitada mas reciente
        if entry["active"] == version:
            for idx in range(len(entry["versions"]) - 1, -1, -1):
                if idx not in entry["disabled"]:
                    entry["active"] = idx
                    break
        vault[secret_name] = entry
        self._save_vault(vault)


# ── Comparacion: ANTI-PATRON vs PATRON SEGURO ────────────────────────────────

# ❌ ANTI-PATRON: credencial hardcodeada
HARDCODED_API_KEY = "sk-abc123-hardcoded-in-code"  # NUNCA hacer esto

# ✅ PATRON SEGURO: secretless (local por defecto, GCP si hay credenciales)
config = SecretlessConfig(project_id=os.environ.get("GCP_PROJECT_ID", "my-project"))


def query_external_api_secure(query: str) -> dict:
    """Tool segura: obtiene credencial del vault (local o Secret Manager) en runtime."""
    api_key = config.get_secret("external-api-key")  # fetch en runtime
    result = {"query": query, "result": "data", "key_used": api_key[:4] + "****"}
    print(f"[tool call ejecutado] query_external_api_secure({query!r}) -> key_used={result['key_used']}")
    return result


# ── Backend de modelo: Ollama local (default) o Gemini en Vertex AI ─────────
GEMINI_MODEL = "gemini-2.5-flash"


def _build_model():
    """LAB_LLM_BACKEND=gemini usa Gemini via Vertex AI (ADC + GCP_PROJECT_ID);
    por defecto usa qwen3.5:9b local via Ollama, sin cambios de comportamiento."""
    if os.environ.get("LAB_LLM_BACKEND", "ollama") == "gemini":
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", os.environ.get("GCP_PROJECT_ID", ""))
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", os.environ.get("GCP_LOCATION", "us-central1"))
        return GEMINI_MODEL
    return LiteLlm(
        model="ollama_chat/qwen3.5:9b",
        num_ctx=8192,
        temperature=0.2,
        reasoning_effort="none",  # fuerza think=False en Ollama, evita el loop de narracion
    )


secretless_agent = Agent(
    name="secretless_agent",
    # Config verificada en Lab 8.1/8.2/10.A/11.A/3.1 -ver docstring del modulo.
    model=_build_model(),
    instruction=(
        # Sin un mandato imperativo, el modelo puede fabricar una respuesta
        # ("no se pudo obtener credenciales sin parametros especificos") sin
        # llegar a invocar la tool -la tool toma el texto de la consulta tal
        # cual como argumento, no hace falta ningun parametro adicional.
        "REGLA OBLIGATORIA, ANTES QUE NADA: para CUALQUIER consulta del "
        "usuario, tenes que llamar a query_external_api_secure pasando el "
        "texto de la consulta como argumento -- ANTES de responder. Nunca "
        "falta esta llamada, incluso si la consulta parece vaga o generica: "
        "usa el texto tal cual te lo escribieron. Responder sin haber "
        "llamado la tool es un error grave -- equivale a inventar un "
        "resultado que nunca se obtuvo.\n\n"
        "Agente con arquitectura secretless: obtenes credenciales del vault "
        "(local o GCP Secret Manager) en runtime via query_external_api_secure, "
        "nunca hardcodeadas.\n\n"
        "Despues de llamar a la herramienta, responde con SOLO el reporte de "
        "abajo, sin narrar tu razonamiento -emiti UNICAMENTE esta plantilla "
        "con los valores reales sustituidos:\n\n"
        "Query: <la query del usuario>\n"
        "Resultado: <el campo 'result' devuelto por la herramienta>\n"
        "Credencial usada: <el campo 'key_used' devuelto -- nunca la clave completa>"
    ),
    tools=[query_external_api_secure],
)

# ── Ejecucion via Runner (patron canonico ADK v2; no existe agent.run("texto")) ─
_session_service = InMemorySessionService()
_runner = Runner(
    agent=secretless_agent, app_name=secretless_agent.name, session_service=_session_service
)


def ask_agent(query: str) -> str:
    session = _session_service.create_session_sync(
        app_name=secretless_agent.name, user_id="student"
    )
    content = types.Content(role="user", parts=[types.Part(text=query)])
    final = ""
    for event in _runner.run(
        user_id="student", session_id=session.id, new_message=content
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final = part.text
    return final


# ── Driver: ejercicios 1-4 ──────────────────────────────────────────────────
def _run_exercises() -> None:
    print("\n--- Ejercicio 1: primer get_secret crea un secreto dummy en el vault local "
          "(o lo trae de Secret Manager / dispara el circuit breaker) ---")
    t0 = time.time()
    key1 = config.get_secret("external-api-key")
    elapsed = time.time() - t0
    print(f"OK: secreto obtenido en {elapsed:.2f}s -> {key1[:4]}**** "
          f"(circuito {'ABIERTO' if config._circuit_open else 'cerrado'}, "
          f"backend GCP {'disponible' if config.client and not config._circuit_open else 'no usado'})")
    assert elapsed < _GCP_TIMEOUT_SECONDS + 2, (
        f"get_secret tardo {elapsed:.1f}s -- el circuit breaker deberia acotar "
        f"esto a ~{_GCP_TIMEOUT_SECONDS}s incluso si GCP esta colgado"
    )

    print("\n--- Ejercicio 2: verificar que el agente usa la herramienta secretless "
          "(via tool call directo, sin Ollama) ---")
    result = query_external_api_secure("seguridad agentica")
    assert result["key_used"] == key1[:4] + "****"
    print(f"OK: {result}")

    print("\n--- Ejercicio 3: rotar el secreto localmente ---")
    config.rotate_secret("external-api-key", "nueva-clave-v2")
    key2 = config.get_secret("external-api-key")
    assert key2 == "nueva-clave-v2", f"esperaba la version rotada, obtuve {key2}"
    print(f"OK: tras rotar, get_secret devuelve automaticamente la nueva version -> {key2}")

    print("\n--- Ejercicio 4: revocar la version anterior ---")
    config.disable_version("external-api-key", version=0)
    key3 = config.get_secret("external-api-key")
    assert key3 == "nueva-clave-v2", "deberia seguir usando la version activa (la rotada)"
    print(f"OK: version 0 revocada; get_secret sigue devolviendo la version habilitada mas reciente -> {key3}")


def _selftest() -> None:
    """Verifica el nucleo determinista (vault local, rotacion, revocacion,
    circuit breaker de GCP) SIN invocar el Agent/Runner -- no toca Ollama en
    ningun momento. Ademas confirma la instanciacion del Agent con
    LiteLlm."""
    assert secretless_agent.name == "secretless_agent"
    if os.environ.get("LAB_LLM_BACKEND", "ollama") == "gemini":
        assert secretless_agent.model == GEMINI_MODEL
        print(f"[selftest] Agent instanciado con Gemini ({GEMINI_MODEL}, Vertex AI) -- OK "
              "(no se llamo al modelo)")
    else:
        assert isinstance(secretless_agent.model, LiteLlm)
        assert secretless_agent.model.model == "ollama_chat/qwen3.5:9b"
        print("[selftest] Agent instanciado con LiteLlm(ollama_chat/qwen3.5:9b) -- OK "
              "(no se llamo a Ollama)")

    if config.client is not None:
        print(f"[selftest] google-cloud-secretmanager disponible, cliente construido. "
              f"Si las ADC estan vencidas, el circuit breaker se activara en el "
              f"Ejercicio 1 (acotado a ~{_GCP_TIMEOUT_SECONDS}s) en vez de colgarse.")
    else:
        print("[selftest] Sin cliente de Secret Manager (libreria no instalada o "
              "sin ADC) -- usando vault local desde el inicio, comportamiento "
              "identico al original.")

    _run_exercises()
    print("\n[selftest] OK: nucleo determinista de Lab 3.3 (vault local, rotacion, "
          "revocacion, circuit breaker) verificado de punta a punta.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)

    print("=" * 70)
    print("Lab 3.3: GCP IAM Secretless (qwen3.5:9b local via Ollama)")
    print("=" * 70)

    _run_exercises()

    print("\n--- Consulta al agente (dispara llamada real a Ollama) ---")
    print(ask_agent("Consulta la API externa sobre seguridad agentica"))
