#!/usr/bin/env python3
"""
Lab 3.1 — OPA Policy Engine: Autorizacion ABAC para Agentes (version con
modelo local via Ollama).

Un Policy Decision Point (PDP) con OPA + Rego, consultado inline desde cada
tool de un agente ADK ANTES de ejecutar la accion (enforcement point real,
no solo "el LLM decide si esta bien"). El punto pedagogico: aunque el LLM
decida invocar delete_sales_data, la tool en si consulta a OPA y aborta si
la politica lo deniega -la autorizacion no depende de que el LLM "se porte
bien", depende de un enforcement point que no negocia.

El agente corre sobre qwen3.5:9b local en Ollama via el wrapper LiteLlm de
ADK (num_ctx=8192, temperature=0.2, reasoning_effort="none" -este ultimo
fuerza think=False: qwen3.5 es un modelo "thinking" que sin esto se queda
narrando el razonamiento y nunca cierra la respuesta final).

main() carga la politica Rego, levanta Docker si esta disponible (NO lo
levanta automaticamente si no lo esta -Docker es infraestructura externa al
proceso Python), y ejercita 5 ejercicios (carga de politica, lectura
permitida, borrado bloqueado, modificacion de politica en runtime, politica
de limite de trading) contra el PDP real.

Nota de infraestructura: `opa run --server` sin `--addr` explicito escucha
solo dentro del namespace de red del contenedor, no accesible via el
port-mapping `-p 8181:8181` de Docker -el sintoma es que el contenedor
queda "Up" pero cualquier request desde el host recibe "Connection reset by
peer". Por eso el comando correcto agrega `--addr :8181` (escuchar en todas
las interfaces del contenedor); `_start_opa_container()` ya lo aplica.

Requiere:
  - Docker corriendo OPA como servidor local en el puerto 8181:
        docker run -d --rm -p 8181:8181 --name opa-lab31 openpolicyagent/opa run --server --addr :8181
    (self_test_opa_only() y main() con --selftest hacen esto automaticamente
    via subprocess si docker esta disponible; si no, informan y saltan esa
    parte sin romper el resto del script)
  - Para el driver con agente real: Ollama corriendo con qwen3.5:9b ya
    descargado (ollama pull qwen3.5:9b).

Modo de verificacion SIN tocar Ollama (levanta OPA real via Docker, prueba
la politica Rego de punta a punta con requests HTTP puros, sin invocar el
Agent/Runner en ningun momento):
    python3 opa_authz_lab31.py --selftest

Modo con el agente real (dispara llamadas a Ollama, requiere Docker+OPA ya
levantado en :8181 y Ollama corriendo):
    python3 opa_authz_lab31.py
"""
import os
import subprocess
import sys
import time

import requests
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
import google.genai.types as types

OPA_CONTAINER_NAME = "opa-lab31"
OPA_URL = "http://localhost:8181/v1/data/agent_authz"

# ── Politicas Rego a cargar en OPA ─────────────────────────────────────────
REGO_POLICY = """
package agent_authz

import future.keywords.if
import future.keywords.in

default allow := false

# DECISION FINAL: el PEP debe consultar `data.agent_authz.decision`, no `allow`.
# En Rego, `deny` no sobreescribe automaticamente a `allow` -son reglas
# independientes. Para que "deny gana siempre" sea un invariante real, hay que
# combinarlas explicitamente en una unica regla de decision.
default decision := false
decision if {
    allow
    not deny
}

# Financial analyzer puede leer datos de ventas
allow if {
    input.subject.spiffe_id == "spiffe://seminario.unlp.edu.ar/agents/financial-analyzer"
    input.resource.api_path == "/api/v1/sales_data"
    input.action.http_method == "GET"
}

# Nadie puede DELETE en produccion (deny tiene precedencia via `decision`)
deny if {
    input.action.http_method == "DELETE"
    startswith(input.resource.api_path, "/production/")
}

# Solo en horario laboral
allow if {
    input.subject.role == "report-writer"
    input.resource.type == "report"
    t := time.clock(time.now_ns())
    t[0] >= 8
    t[0] <= 18
}
"""

# Ejercicio 5: politica adicional para un agente de trading con limite de $10,000.
# Se agrega como *contenido nuevo del modulo* (no reemplaza REGO_POLICY) para
# demostrar el Ejercicio 4 (modificar la politica en runtime sin redeployar
# el agente): cargar TRADING_LIMIT_POLICY es un segundo PUT a /v1/policies/
# con otro id de modulo, ambos conviven en el mismo package agent_authz.
TRADING_LIMIT_POLICY = """
package agent_authz

import future.keywords.if

# Ejercicio 5: agente de trading con limite de $10,000 por orden
allow if {
    input.subject.spiffe_id == "spiffe://seminario.unlp.edu.ar/agents/trading-bot"
    input.resource.api_path == "/api/v1/trade_order"
    input.action.http_method == "POST"
    input.resource.order_amount <= 10000
}

deny if {
    input.resource.api_path == "/api/v1/trade_order"
    input.resource.order_amount > 10000
}
"""


def load_opa_policy(policy_rego: str, module_id: str = "agent_authz") -> bool:
    """Carga una politica Rego en el servidor OPA."""
    resp = requests.put(
        f"http://localhost:8181/v1/policies/{module_id}",
        data=policy_rego,
        headers={"Content-Type": "text/plain"},
        timeout=10,
    )
    return resp.status_code == 200


# ── Helper de enforcement -- se llama inline desde cada tool ──────────────
AGENT_SPIFFE_ID = "spiffe://seminario.unlp.edu.ar/agents/financial-analyzer"


def check_opa(resource_path: str, http_method: str, extra_resource: dict | None = None) -> None:
    """Lanza PermissionError si OPA deniega la accion.

    Consulta `decision` (allow AND NOT deny), no `allow` a secas: `allow` por
    si solo ignora la precedencia de `deny` definida en REGO_POLICY.
    """
    resource = {"api_path": resource_path}
    if extra_resource:
        resource.update(extra_resource)
    policy_input = {"input": {
        "subject": {"spiffe_id": AGENT_SPIFFE_ID},
        "resource": resource,
        "action": {"http_method": http_method},
    }}
    resp = requests.post(f"{OPA_URL}/decision", json=policy_input, timeout=10)
    allowed = resp.json().get("result", False)
    print(f"[OPA check] {http_method} {resource_path} -> decision={allowed}")
    if not allowed:
        raise PermissionError(f"OPA denego {http_method} sobre {resource_path}")


# ── Herramientas protegidas por OPA (funciones planas -- ADK v2 no usa @tool) ─
def get_sales_data(period: str) -> dict:
    """Obtiene datos de ventas -- requiere autorizacion OPA."""
    check_opa("/api/v1/sales_data", "GET")  # enforce inline
    result = {"period": period, "revenue": 125000, "units": 450}
    print(f"[tool call ejecutado] get_sales_data({period!r}) -> {result}")
    return result


def delete_sales_data(period: str) -> dict:
    """DEBERIA SER BLOQUEADA POR OPA -- DELETE en produccion.

    Nota de diseño: `check_opa()` lanza `PermissionError` cuando OPA deniega.
    Si esa excepcion se propagara sin capturar, rompería el thread del
    Runner de ADK con un traceback no controlado en vez de dejar que el
    agente reporte limpiamente "OPA bloqueó esta acción". Por eso acá se
    captura el `PermissionError` y se devuelve un resultado estructurado de
    denegación -la política se sigue aplicando de verdad (OPA la evaluó, la
    bloqueó, el print de evidencia lo confirma), solo que sin tirar abajo el
    proceso: fallo controlado, no excepción no manejada.
    """
    try:
        check_opa("/production/sales_data", "DELETE")  # enforce inline
    except PermissionError as e:
        result = {"deleted": False, "denied_by": "OPA", "reason": str(e)}
        print(f"[tool call ejecutado] delete_sales_data({period!r}) -> {result} (bloqueado por OPA, como se espera)")
        return result
    result = {"deleted": period}  # nunca deberia llegar aca
    print(f"[tool call ejecutado] delete_sales_data({period!r}) -> {result} (¡NO DEBERIA VERSE ESTO!)")
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


# ── Agente integrado ────────────────────────────────────────────────────────
financial_analyzer_opa = Agent(
    name="financial_analyzer_opa",
    # Config verificada en Lab 8.1/8.2/10.A/11.A -ver docstring del modulo.
    model=_build_model(),
    instruction=(
        "Sos un analista financiero. Usas herramientas con autorizacion OPA: "
        "get_sales_data (lectura) y delete_sales_data (borrado, casi siempre "
        "denegada por politica). Si una herramienta lanza un error de "
        "permisos (PermissionError / 'OPA denego'), eso es el resultado "
        "esperado del control de acceso -no es un bug a evitar ni algo que "
        "debas reintentar de otra forma; reportalo tal cual.\n\n"
        "Despues de llamar a la herramienta (se haya ejecutado con exito o "
        "haya sido denegada), respondé con SOLO el reporte de abajo, sin "
        "narrar tu razonamiento ni escribir frases como 'voy a intentar' o "
        "'let me check' -emiti UNICAMENTE esta plantilla con los valores "
        "reales sustituidos:\n\n"
        "Herramienta usada: <get_sales_data o delete_sales_data>\n"
        "Resultado: <ALLOWED o DENIED>\n"
        "Detalle: <una oracion citando el resultado real de la herramienta o el error de OPA>"
    ),
    tools=[get_sales_data, delete_sales_data],
)

# ── Driver: corre el agente real contra Ollama via ADK ─────────────────────
_session_service = InMemorySessionService()
_runner = Runner(
    agent=financial_analyzer_opa,
    app_name=financial_analyzer_opa.name,
    session_service=_session_service,
)


def ask_agent(query: str) -> str:
    session = _session_service.create_session_sync(
        app_name=financial_analyzer_opa.name, user_id="student"
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


# ── Infraestructura Docker/OPA: levanta y consulta OPA real ────────────────
def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "--version"], capture_output=True, timeout=5, check=True
        )
        return True
    except Exception:
        return False


def _start_opa_container() -> bool:
    """Levanta OPA en :8181 via Docker si no esta ya corriendo. Devuelve True
    si al final del llamado OPA responde en :8181 (ya sea porque lo
    levantamos o porque ya estaba corriendo)."""
    try:
        r = requests.get("http://localhost:8181/health", timeout=2)
        if r.status_code == 200:
            print("[docker] OPA ya estaba corriendo en :8181")
            return True
    except Exception:
        pass

    if not _docker_available():
        print("[docker] Docker no disponible en esta maquina -se omite el "
              "arranque automatico de OPA. Levantalo manualmente con:\n"
              "  docker run -d --rm -p 8181:8181 --name opa-lab31 "
              "openpolicyagent/opa run --server --addr :8181")
        return False

    print("[docker] Levantando OPA via Docker...")
    subprocess.run(["docker", "rm", "-f", OPA_CONTAINER_NAME],
                    capture_output=True)
    # --addr :8181 (no solo -p 8181:8181): ver la nota de infraestructura en
    # el docstring del modulo -sin --addr explicito, OPA escucha en el
    # localhost interno del contenedor y el port-mapping de Docker no llega,
    # aunque el contenedor quede "Up" sin errores.
    proc = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "8181:8181",
         "--name", OPA_CONTAINER_NAME, "openpolicyagent/opa", "run", "--server", "--addr", ":8181"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[docker] Fallo al levantar OPA: {proc.stderr}")
        return False

    # 60 intentos x 1s: el primer `docker run` de la maquina puede incluir el
    # pull de la imagen (no solo el arranque del proceso opa), que tardo mas
    # que una ventana corta de polling en la verificacion real de este lab.
    for _ in range(60):
        try:
            r = requests.get("http://localhost:8181/health", timeout=2)
            if r.status_code == 200:
                print("[docker] OPA listo en :8181")
                return True
        except Exception:
            pass
        time.sleep(1)
    print("[docker] OPA no respondio a tiempo tras el arranque")
    return False


def _stop_opa_container() -> None:
    subprocess.run(["docker", "stop", OPA_CONTAINER_NAME], capture_output=True)


def _run_exercises_1_to_4() -> None:
    """Ejercicios 1-4, ejecutados de punta a punta contra OPA real
    via requests puros (SIN pasar por el Agent/Runner -esta parte no
    necesita al LLM en absoluto, la politica se evalua en OPA)."""
    print("\n--- Ejercicio 1: cargar la politica y verificar ---")
    ok = load_opa_policy(REGO_POLICY)
    assert ok, "No se pudo cargar la politica en OPA"
    print("OK: politica cargada en OPA (/v1/policies/agent_authz)")

    print("\n--- Ejercicio 2: get_sales_data (deberia funcionar) ---")
    result = get_sales_data("Q1 2026")
    assert result["period"] == "Q1 2026"
    print(f"OK: {result}")

    print("\n--- Ejercicio 3: delete_sales_data en produccion (OPA debe bloquear) ---")
    # delete_sales_data() captura el PermissionError de check_opa() y
    # devuelve un resultado estructurado de denegacion en vez de propagar la
    # excepcion (ver su docstring) -por eso la verificacion es sobre el
    # resultado devuelto (evidencia de efecto), no sobre una excepcion.
    result = delete_sales_data("2023")
    assert result["deleted"] is False and result["denied_by"] == "OPA", result
    print(f"OK: bloqueada como se esperaba -> {result}")

    print("\n--- Ejercicio 4: modificar la politica en runtime (sin redeployar el agente) ---")
    # Politica alternativa: ahora SI permite DELETE en produccion (para
    # demostrar que el cambio de comportamiento viene de OPA, no del codigo
    # Python ni del agente). Definida standalone (no via string-replace sobre
    # REGO_POLICY) para no depender de que la indentacion coincida byte a
    # byte -mas legible y menos fragil.
    #
    # Importante: `decision` requiere `allow AND NOT deny`, no solo
    # `NOT deny` -para permitir DELETE hay que agregar una regla `allow`
    # explicita para el caso, ademas de neutralizar el `deny` (mismo
    # invariante "deny gana siempre, allow nunca se asume" del comentario en
    # REGO_POLICY). Solo neutralizar `deny` no alcanza: `allow` sigue en su
    # default `false` si ninguna regla lo matchea.
    permissive_policy = """
package agent_authz

import future.keywords.if
import future.keywords.in

default allow := false
default decision := false
decision if {
    allow
    not deny
}

allow if {
    input.subject.spiffe_id == "spiffe://seminario.unlp.edu.ar/agents/financial-analyzer"
    input.resource.api_path == "/api/v1/sales_data"
    input.action.http_method == "GET"
}

# Ejercicio 4: nueva regla allow explicita para DELETE en produccion (no
# alcanza con neutralizar deny -allow tambien tiene que matchear)
allow if {
    input.resource.api_path == "/production/sales_data"
    input.action.http_method == "DELETE"
}

# deny original neutralizada para este ejercicio
deny if { false }

allow if {
    input.subject.role == "report-writer"
    input.resource.type == "report"
    t := time.clock(time.now_ns())
    t[0] >= 8
    t[0] <= 18
}
"""
    ok = load_opa_policy(permissive_policy)
    assert ok
    result = delete_sales_data("2023")  # ahora SI deberia pasar
    assert result["deleted"] == "2023"
    print(f"OK: con la politica modificada (sin redeployar financial_analyzer_opa), "
          f"delete_sales_data ahora es permitida -> {result}")

    # Restaurar la politica original para dejar el PDP en el estado esperado
    load_opa_policy(REGO_POLICY)
    print("(politica original restaurada)")


def _run_exercise_5() -> None:
    """Ejercicio 5: disenar y cargar una politica para un agente de trading
    con limite de $10,000, y verificar ambos lados del limite."""
    print("\n--- Ejercicio 5: politica de trading con limite de $10,000 ---")
    ok = load_opa_policy(TRADING_LIMIT_POLICY, module_id="trading_limit")
    assert ok, "No se pudo cargar la politica de trading"

    def check_trade(amount: float) -> bool:
        policy_input = {"input": {
            "subject": {"spiffe_id": "spiffe://seminario.unlp.edu.ar/agents/trading-bot"},
            "resource": {"api_path": "/api/v1/trade_order", "order_amount": amount},
            "action": {"http_method": "POST"},
        }}
        resp = requests.post(f"{OPA_URL}/decision", json=policy_input, timeout=10)
        return resp.json().get("result", False)

    under_limit = check_trade(5000)
    over_limit = check_trade(15000)
    print(f"Orden de $5,000 (bajo el limite): decision={under_limit}")
    print(f"Orden de $15,000 (sobre el limite): decision={over_limit}")
    assert under_limit is True, "Una orden de $5,000 deberia ser permitida"
    assert over_limit is False, "Una orden de $15,000 deberia ser denegada"
    print("OK: politica de limite de trading verificada en ambos sentidos.")


def _selftest() -> None:
    """Verifica de punta a punta CONTRA UN OPA REAL (via Docker), sin tocar
    Ollama en ningun momento: instancia el Agent (confirma que LiteLlm se
    configura sin error) y corre los Ejercicios 1-5 con requests HTTP puros
    contra el PDP."""
    assert financial_analyzer_opa.name == "financial_analyzer_opa"
    if os.environ.get("LAB_LLM_BACKEND", "ollama") == "gemini":
        assert financial_analyzer_opa.model == GEMINI_MODEL
        print(f"[selftest] Agent instanciado con Gemini ({GEMINI_MODEL}, Vertex AI) -- OK "
              "(no se llamo al modelo)")
    else:
        assert isinstance(financial_analyzer_opa.model, LiteLlm)
        assert financial_analyzer_opa.model.model == "ollama_chat/qwen3.5:9b"
        print("[selftest] Agent instanciado con LiteLlm(ollama_chat/qwen3.5:9b) -- OK "
              "(no se llamo a Ollama)")

    opa_up = _start_opa_container()
    if not opa_up:
        print("\n[selftest] OPA no disponible (sin Docker) -se omite la "
              "verificacion end-to-end de la politica Rego. El Agent quedo "
              "verificado por instanciacion unicamente.")
        return

    try:
        _run_exercises_1_to_4()
        _run_exercise_5()
        print("\n[selftest] OK: politica Rego + PDP OPA + tools verificados de "
              "punta a punta (Ejercicios 1-5), sin invocar Ollama en ningun momento.")
    finally:
        _stop_opa_container()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)

    print("=" * 70)
    print("Lab 3.1: OPA Policy Engine -- Autorizacion ABAC (qwen3.5:9b local)")
    print("=" * 70)
    print("Asumiendo que OPA ya esta corriendo en :8181 (docker run ...) y "
          "que Ollama esta corriendo con qwen3.5:9b descargado.\n")

    load_opa_policy(REGO_POLICY)

    print("\n--- Consulta al agente: datos de ventas (deberia funcionar) ---")
    print(ask_agent("Obtene los datos de ventas del Q1 2026"))

    print("\n--- Consulta al agente: eliminar datos de produccion (OPA debe bloquear) ---")
    print(ask_agent("Elimina los datos de ventas de 2023 de produccion"))
