#!/usr/bin/env python3
"""
Lab Propuesto 2.1: MAESTRO Automatico -- Generador de Threat Models con ADK,
version GCP / Gemini via Vertex AI.

Adaptado del "Codigo esqueleto" de ch02-labs.md (lineas 36-164). Es el lab
mas dificil del capitulo: combina `output_schema=MaestroThreatModel`
(Pydantic) CON `tools`, en la generacion mas larga de todo el set (7 capas +
amenazas por capa + amenazas cross-layer + top-5 riesgos), y el libro
parsea el resultado con `MaestroThreatModel.model_validate_json(result_text)`
SIN try/except -- cualquier drift de schema tira abajo el lab entero con una
excepcion no controlada. Esta version GCP mantiene el parseo robusto y el
fallback de dos niveles agregados en la migracion a Ollama (ver mas abajo),
porque son buenas practicas generales, no fixes especificos de un modelo
local.

===========================================================================
HALLAZGO PRINCIPAL EN LA VERSION OLLAMA: output_schema + tools con LiteLlm
===========================================================================

La migracion a Ollama (labs/ch02-lab1-ollama/) encontro, CONFIRMADO en una
corrida real contra qwen3.5:9b, que `Agent(output_schema=X, tools=[...],
model=LiteLlm(...))` se instancia sin excepcion, pero el modelo devolvia un
JSON perfectamente valido SIN haber llamado ninguna tool -- un threat model
100% alucinado pero bien formado. La causa raiz identificada por lectura de
codigo de ADK: `google.adk.utils.output_schema_utils.
can_use_output_schema_with_tools(model)` devuelve `True` INCONDICIONALMENTE
para cualquier instancia de `LiteLlm`, lo que hace que ADK mande
`output_schema` (via el campo `format` de Ollama) Y `tools` en el MISMO
request, en TODOS los turnos -- incluidos los turnos donde se espera que el
modelo llame una tool, no que cierre el JSON final. Con un modelo 9B en CPU,
esas dos restricciones estructurales compitiendo en simultaneo resultaron en
que el modelo cortara camino directo al JSON sin pasar por el razonamiento
por capas.

===========================================================================
QUE CAMBIA CON GEMINI NATIVO (verificado por lectura de codigo, sin llamadas
reales -- ver diagnostico abajo)
===========================================================================

Se verifico en este entorno, SIN tocar Vertex AI/red (solo inspeccionando el
objeto Python), el mismo chequeo que hace ADK para decidir si combina
output_schema+tools "a la Ollama" o si usa el workaround por prompt:

    from google.adk.models.google_llm import Gemini
    from google.adk.utils.output_schema_utils import can_use_output_schema_with_tools
    can_use_output_schema_with_tools(Gemini(model="gemini-2.0-flash"))  # -> False

A diferencia de LiteLlm (que siempre da True), para un modelo `Gemini`
nativo esta funcion devuelve **False**. Eso significa que ADK NO manda
output_schema+tools juntos en el mismo request a la API de Gemini: en su
lugar, activa el mecanismo de compatibilidad "prompt-based" documentado en
`google.adk.flows.llm_flows._output_schema_processor` -- inyecta una tool
extra (`set_model_response`) que el modelo debe llamar para emitir la
respuesta final estructurada, en vez de competir con el resto de las tools
por decoding con schema en el mismo turno. Es un CAMINO DE CODIGO DISTINTO
al que causo el hallazgo en Ollama, no la misma mecanica con un modelo mas
grande. Conclusion (sin verificar con una llamada real): el riesgo especifico
documentado para Ollama (schema-constrained decoding de la libreria del
proveedor + tools compitiendo en el mismo POST) NO deberia aplicar tal cual a
Gemini nativo, porque ADK nunca arma ese request combinado en primer lugar
para este tipo de modelo. Dicho eso, el mecanismo `set_model_response` tiene
sus propios riesgos no explorados (¿el modelo puede "fingir" llamar a
set_model_response con un resultado fabricado, sin haber llamado antes a las
tools de analisis? -no se investigo el codigo de
`_output_schema_processor.py` en profundidad para esta migracion).

DISENO DE ESTE ARCHIVO: por eso se mantiene la MISMA arquitectura de 2
niveles que la version Ollama, como red de contencion, no como algo que se
espera activar con Gemini:
  1. `maestro_agent` (nativo, output_schema+tools) como primer intento.
  2. Parseo envuelto en try/except con extraccion robusta de JSON (bloques
     ```json, texto antes/despues del JSON) en vez del `model_validate_json`
     desnudo del libro.
  3. Criterio de exito que exige EVIDENCIA de tool-calling real
     (`_LAYERS_ANALYZED == {1..7}`), no solo que el JSON haya parseado bien
     -- este criterio se mantiene igual con Gemini, porque protege contra
     CUALQUIER forma de "JSON valido pero sin analisis real", sea cual sea
     la causa (Ollama, el mecanismo set_model_response, o algo no previsto).
  4. Si el intento nativo falla el criterio, cae automaticamente a
     `maestro_agent_manual` (sin output_schema nativo, schema en texto,
     parseo manual).
  5. Si TAMBIEN falla el fallback manual, el lab NO revienta con una
     excepcion no controlada: devuelve un resultado explicito de fallo.

Requiere: google-adk, pydantic, y credenciales de Vertex AI (ADC de gcloud
con GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION exportadas, o GOOGLE_GENAI_
USE_VERTEXAI=true) o una GOOGLE_API_KEY de AI Studio -segun se decida al
correr el lab.

Modo de verificacion sin llamadas reales (instanciacion de ambos agentes +
logica de extraccion/parseo robusta, con textos canned que simulan salidas
buenas y malas de un modelo -- sin tocar Vertex AI):
    python3 maestro_threat_model_lab21.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from typing import Dict, List, Optional, Tuple

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
import google.genai.types as types
from pydantic import BaseModel, ValidationError

# Modelo Gemini via ADK -- string plano, ADK decide Vertex AI vs AI Studio
# segun las env vars definidas al correr el lab. El proyecto GCP todavia no
# esta decidido: no hardcodear ningun project id.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


# ── Estructura del Threat Model (identica al libro) ─────────────────────
class ThreatItem(BaseModel):
    layer: int
    threat_name: str
    description: str
    agentic_factor: str  # non-determinism | autonomy | identity | a2a-comm
    likelihood: str      # low | medium | high
    impact: str           # low | medium | high
    mitigation: str


class LayerAnalysis(BaseModel):
    """Analisis de una capa MAESTRO individual (clave = numero de capa como string)."""
    technology: str
    threats: List[str]
    severity: str


class CrossLayerThreat(BaseModel):
    """Amenaza que encadena dos o mas capas MAESTRO."""
    layers_involved: List[int]
    threat_name: str
    description: str
    severity: str


class MaestroThreatModel(BaseModel):
    system_name: str
    layer_decomposition: Dict[str, LayerAnalysis]  # "1".."7" -> analisis de la capa
    layer_threats: List[ThreatItem]
    cross_layer_threats: List[CrossLayerThreat]
    top_risks: List[ThreatItem]  # top 5 por likelihood * impact


# ── Herramientas (identicas al libro, + evidencia de efecto) ────────────
MAESTRO_LAYERS = {
    1: "Foundation Models", 2: "Data Operations", 3: "Agent Frameworks",
    4: "Deployment Infrastructure", 5: "Evaluation & Observability",
    6: "Security & Compliance", 7: "Agent Ecosystem",
}


# Evidencia de arity, no solo de que la tool se llamo: --selftest no invoca
# al LLM, asi que esto solo se llena en corridas reales contra Vertex AI --
# pero es la senal mas util para diagnosticar si el modelo corto camino al
# JSON final sin pasar por las 7 capas (ver docstring del modulo).
_LAYERS_ANALYZED: set = set()

# Deduplicacion por codigo (no por prompt): en la version Ollama, pedirle al
# modelo por prompt "no repitas la misma llamada" no alcanzo para cortar un
# loop -- el agente fallback quedo repitiendo indefinidamente los mismos 2
# pares de capas. Se mantiene la misma tool con deduplicacion aca, como red
# de contencion general (no depende de que Gemini tenga o no el mismo
# problema, que no fue verificado).
_CROSS_LAYER_SEEN: set = set()


def analyze_layer(layer_num: int, system_context: str) -> dict:
    """Resuelve el nombre canonico de una capa MAESTRO (1-7) y devuelve el contexto
    del sistema para que el modelo razone sobre amenazas especificas de esa capa."""
    layer_name = MAESTRO_LAYERS.get(layer_num, "capa desconocida")
    result = {"layer": layer_num, "layer_name": layer_name, "system_context": system_context}
    _LAYERS_ANALYZED.add(layer_num)
    print(f"[tool call ejecutado] analyze_layer({layer_num}) -> layer_name={layer_name!r}")
    return result


def identify_cross_layer_threats(layers_involved: List[int]) -> dict:
    """Marca una combinacion de capas (ej. [1, 3, 6]) como candidata a amenaza
    cross-layer, devolviendo sus nombres para que el modelo describa el encadenamiento.

    Deduplica por codigo (ver nota arriba de _CROSS_LAYER_SEEN): si la misma
    combinacion ya se reporto antes, en vez de repetir el resultado devuelve
    una instruccion explicita de avanzar, para cortar loops donde el modelo
    reinvoca la misma tool indefinidamente en vez de progresar al JSON final.
    """
    key = tuple(sorted(set(layers_involved)))
    names = [MAESTRO_LAYERS.get(l, str(l)) for l in layers_involved]
    if key in _CROSS_LAYER_SEEN:
        print(f"[tool call ejecutado] identify_cross_layer_threats({layers_involved}) -> "
              f"DUPLICADO, ya reportado -- devolviendo nudge para avanzar")
        return {
            "layers_involved": layers_involved,
            "layer_names": names,
            "already_reported": True,
            "instruction": (
                "Esta combinacion de capas YA fue identificada antes en esta sesion. "
                "No la vuelvas a llamar. Si ya cubriste las combinaciones cross-layer "
                "relevantes, DEJA de llamar tools y escribi el JSON final ahora."
            ),
        }
    _CROSS_LAYER_SEEN.add(key)
    result = {"layers_involved": layers_involved, "layer_names": names}
    print(f"[tool call ejecutado] identify_cross_layer_threats({layers_involved}) -> {names}")
    return result


# ── Plantilla textual del schema esperado (fuente unica: campos 1:1 con ──
# MaestroThreatModel; se usa en la instruccion del agente fallback manual, y
# tambien como referencia extra en el agente nativo). Se escribe a mano (no
# MaestroThreatModel.model_json_schema() crudo) porque el JSON Schema de
# Pydantic trae $defs/$ref/additionalProperties -- ruido que un modelo
# interpreta peor que un ejemplo concreto.
MAESTRO_JSON_TEMPLATE = """{
  "system_name": "<string>",
  "layer_decomposition": {
    "1": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "2": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "3": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "4": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "5": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "6": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "7": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"}
  },
  "layer_threats": [
    {"layer": 1, "threat_name": "<string>", "description": "<string>",
     "agentic_factor": "<non-determinism|autonomy|identity|a2a-comm>",
     "likelihood": "<low|medium|high>", "impact": "<low|medium|high>", "mitigation": "<string>"}
  ],
  "cross_layer_threats": [
    {"layers_involved": [1, 3, 6], "threat_name": "<string>", "description": "<string>", "severity": "<low|medium|high>"}
  ],
  "top_risks": [
    {"layer": 1, "threat_name": "<string>", "description": "<string>",
     "agentic_factor": "<non-determinism|autonomy|identity|a2a-comm>",
     "likelihood": "<low|medium|high>", "impact": "<low|medium|high>", "mitigation": "<string>"}
  ]
}"""

PROCESS_INSTRUCTION = """Eres un experto en threat modeling de sistemas agenticos usando el framework MAESTRO.

REGLA OBLIGATORIA, ANTES QUE NADA: para CUALQUIER pedido de threat modeling,
tenes que llamar a analyze_layer UNA VEZ POR CADA UNA de las 7 capas (1 a 7)
ANTES de escribir tu respuesta final, y despues llamar a
identify_cross_layer_threats al menos una vez con las capas que encadenen
una amenaza cross-layer real. Nunca faltan estas llamadas: no importa que
tan simple parezca el sistema descripto, siempre analizas las 7 capas.
Generar un threat model sin haber llamado analyze_layer para las 7 capas es
un error grave -- equivale a inventar un analisis que nunca se hizo.

PROCESO A SEGUIR (6 pasos del modulo 2):
1. DESCOMPONER el sistema en las 7 capas MAESTRO, identificando tecnologias especificas
2. IDENTIFICAR amenazas por capa considerando los 4 factores agenticos:
   - Non-determinism: comportamiento emergente del LLM
   - Autonomy: acciones autonomas sin supervision humana
   - Identity Management: gestion de identidades de agentes
   - Agent-to-Agent Communication: comunicacion entre agentes
3. IDENTIFICAR amenazas cross-layer usando identify_cross_layer_threats -- UNA
   SOLA VEZ por cada par de capas distinto que encadene una amenaza real, no
   repitas la misma llamada con las mismas capas mas de una vez
4. EVALUAR riesgo (likelihood x impact) para cada amenaza
5. PLANIFICAR mitigaciones (preventivas + detectivas + responsivas)
6. RECOMENDAR plan de monitoreo continuo

Usa analyze_layer para consultar el nombre canonico de cada capa antes de describir
sus amenazas."""

NATIVE_INSTRUCTION = (
    PROCESS_INSTRUCTION
    + "\n\nTu respuesta final debe ajustarse EXACTAMENTE al esquema "
    "MaestroThreatModel, con esta forma (referencia, no la copies literal, "
    "completa los valores reales del analisis):\n\n"
    + MAESTRO_JSON_TEMPLATE
)

MANUAL_INSTRUCTION = (
    PROCESS_INSTRUCTION
    + "\n\nDespues de llamar todas las tools necesarias, tu respuesta final "
    "debe ser SOLO un objeto JSON valido (sin bloques ```json, sin texto "
    "antes ni despues, sin comentarios) que cumpla EXACTAMENTE esta forma "
    "(referencia de la estructura -- completa los valores reales de tu "
    "analisis, no copies los placeholders <...> literalmente):\n\n"
    + MAESTRO_JSON_TEMPLATE
    + "\n\nSi no emitis JSON valido y completo, el lab no puede procesar tu "
    "respuesta -- es un error grave."
)


# generate_content_config con temperature baja: util para tareas de
# generacion larga y estructurada como esta (7 capas + amenazas + top-5),
# donde mas consistencia entre corridas ayuda a comparar resultados en
# clase. No es un requisito tecnico de Gemini (a diferencia de
# reasoning_effort="none" con qwen3.5, que evitaba un loop de narracion) --
# es una eleccion de diseno general, opcional.
def _make_generate_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=0.2)


maestro_agent = Agent(
    name="maestro_threat_modeler",
    model=GEMINI_MODEL,
    generate_content_config=_make_generate_config(),
    description="Aplica el framework MAESTRO para generar threat models de sistemas agenticos.",
    instruction=NATIVE_INSTRUCTION,
    tools=[analyze_layer, identify_cross_layer_threats],
    output_schema=MaestroThreatModel,
)

# Agente ALTERNATIVO (sin output_schema nativo): mismo rol y mismas tools,
# pero el schema esperado se describe en texto dentro de la instruccion y el
# parseo es 100% manual del lado de Python. Es el fallback automatico si
# `maestro_agent` no produce un JSON parseable -- ver build_threat_model().
maestro_agent_manual = Agent(
    name="maestro_threat_modeler_manual",
    model=GEMINI_MODEL,
    generate_content_config=_make_generate_config(),
    description="Version fallback de maestro_threat_modeler sin output_schema nativo.",
    instruction=MANUAL_INSTRUCTION,
    tools=[analyze_layer, identify_cross_layer_threats],
)


# ── Driver ADK (Runner canonico) ─────────────────────────────────────────
def ask_agent(agent: Agent, prompt: str, app_name: str) -> str:
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        app_name=app_name, user_id="student", session_id=str(uuid.uuid4())
    )
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    result_text = ""
    for event in runner.run(user_id="student", session_id=session.id, new_message=content):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    result_text = part.text
    return result_text


# ── Parseo robusto (reemplaza el model_validate_json desnudo del libro) ──
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_candidate(text: str) -> Optional[str]:
    """Intenta aislar el objeto JSON de una respuesta de modelo que puede
    traer texto extra alrededor (bloque ```json ... ```, o prosa antes/
    despues del JSON). No usa json.loads todavia -- solo recorta el string
    candidato mas prometedor para que el llamador lo valide."""
    text = text.strip()
    if not text:
        return None

    # 1) Bloque de codigo ```json ... ``` o ``` ... ```
    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 2) Substring balanceado desde el primer '{' hasta su '}' de cierre
    # (cuenta llaves respetando strings entre comillas, para no cortar en
    # una '}' que aparezca dentro de un valor de texto).
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # nunca cerro -- JSON truncado (respuesta cortada a mitad)


def try_parse_threat_model(result_text: str) -> Tuple[Optional[MaestroThreatModel], str]:
    """Intenta parsear result_text como MaestroThreatModel con 2 estrategias
    antes de rendirse. Nunca deja escapar ValidationError/JSONDecodeError --
    devuelve (None, motivo) en vez de reventar el proceso del llamador."""
    if not result_text or not result_text.strip():
        return None, "respuesta vacia del modelo"

    # Estrategia 1: el texto ya es JSON valido de punta a punta (caso feliz,
    # el que asume el codigo del libro sin red de seguridad).
    try:
        return MaestroThreatModel.model_validate_json(result_text.strip()), "parseo directo"
    except (ValidationError, json.JSONDecodeError):
        pass

    # Estrategia 2: extraer el JSON de en medio de fences/prosa y reintentar.
    candidate = _extract_json_candidate(result_text)
    if candidate:
        try:
            return MaestroThreatModel.model_validate_json(candidate), "parseo tras extraccion"
        except (ValidationError, json.JSONDecodeError) as e:
            return None, f"JSON extraido pero invalido para el schema: {e}"

    return None, "no se encontro un objeto JSON balanceado en la respuesta"


# ── Driver de alto nivel: nativo -> fallback manual -> fallo controlado ──
def build_threat_model(
    system_description: str,
    ask_fn=ask_agent,
) -> Tuple[Optional[MaestroThreatModel], str, str]:
    """Devuelve (modelo_o_none, modo_usado, texto_crudo_del_ultimo_intento).

    modo_usado in {"native", "manual_fallback", "failed"}. Nunca propaga una
    excepcion de parseo -- a diferencia del codigo del libro
    (MaestroThreatModel.model_validate_json(result_text) sin try/except),
    que tira abajo el proceso entero ante cualquier drift de schema.

    `ask_fn` es inyectable (default: `ask_agent`, que SI invoca Runner/Vertex
    AI de verdad) precisamente para que --selftest pueda probar esta funcion
    con un doble de prueba que nunca toca la red -- ver _self_test(). Un
    monkeypatch por reimportacion de modulo (`import este_mismo_archivo`)
    NO sirve para interceptar esto: crea un objeto de modulo nuevo con su
    propio namespace global, separado del que ve esta funcion -- exactamente
    el bug que causo una invocacion real a Ollama sin querer durante el
    desarrollo de la version original de este lab. La inyeccion por
    parametro es la forma correcta de evitarlo, y aca importa todavia mas:
    una llamada real accidental seria una llamada FACTURADA a Vertex AI, no
    solo CPU gastada como en el caso de Ollama.
    """
    prompt = f"Realizar threat modeling MAESTRO completo para: {system_description}"

    print("[build_threat_model] intento 1/2: agente nativo (output_schema+tools)...")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()
    native_text = ask_fn(maestro_agent, prompt, "maestro-native")
    model, reason = try_parse_threat_model(native_text)
    # Criterio de exito que exige evidencia de tool-calling real, no solo
    # que el JSON haya parseado bien -- se mantiene igual que en la version
    # Ollama (donde este fue el fix real de un hallazgo confirmado en vivo:
    # el modelo devolvio un JSON valido sin haber llamado analyze_layer ni
    # una vez). Con Gemini nativo el mecanismo interno es distinto (ver
    # docstring del modulo: can_use_output_schema_with_tools(Gemini) es
    # False, asi que ADK usa el workaround set_model_response en vez de
    # competir output_schema+tools en el mismo request) -- pero el criterio
    # de exito basado en evidencia real protege igual, sin importar cual sea
    # el mecanismo interno que eventualmente falle.
    layers_ok = _LAYERS_ANALYZED == set(range(1, 8))
    if model is not None and layers_ok:
        print(f"[build_threat_model] OK con agente nativo ({reason}). "
              f"Capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} (esperado: 1-7 completo).")
        return model, "native", native_text

    if model is not None and not layers_ok:
        print(f"[build_threat_model] agente nativo produjo JSON valido PERO sin evidencia de "
              f"analisis real -- capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} "
              f"(esperado: 1-7 completo). Tratando como fallo (threat model alucinado, no confiable).")
    else:
        print(f"[build_threat_model] agente nativo NO produjo un threat model valido: {reason}")
        print(f"[build_threat_model] Capas analizadas via tool antes de fallar: {sorted(_LAYERS_ANALYZED)}")
    print("[build_threat_model] intento 2/2: agente fallback (schema en texto, parseo manual)...")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()
    manual_text = ask_fn(maestro_agent_manual, prompt, "maestro-manual")
    model, reason = try_parse_threat_model(manual_text)
    layers_ok = _LAYERS_ANALYZED == set(range(1, 8))
    if model is not None and layers_ok:
        print(f"[build_threat_model] OK con agente fallback ({reason}). "
              f"Capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} (esperado: 1-7 completo).")
        return model, "manual_fallback", manual_text
    if model is not None and not layers_ok:
        # No queda otro intento -- devolvemos el resultado pero marcado como
        # no verificado, en vez de esconder que el analisis puede estar
        # incompleto o alucinado (mismo principio que arriba: JSON valido no
        # es lo mismo que analisis real).
        print(f"[build_threat_model] agente fallback produjo JSON valido PERO con evidencia "
              f"incompleta -- capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} "
              f"(esperado: 1-7 completo). Devolviendo igual (ultimo intento disponible), "
              f"marcado como no verificado.")
        return model, "manual_fallback_unverified", manual_text

    print(f"[build_threat_model] agente fallback TAMBIEN fallo: {reason}")
    print(f"[build_threat_model] Capas analizadas via tool antes de fallar: {sorted(_LAYERS_ANALYZED)}")
    print("[build_threat_model] FALLO CONTROLADO -- se devuelve diagnostico en vez de excepcion.")
    return None, "failed", f"--- nativo ---\n{native_text}\n\n--- fallback ---\n{manual_text}"


def print_threat_model_report(threat_model: MaestroThreatModel) -> None:
    print(f"Threat model para: {threat_model.system_name}")
    print(f"Amenazas por capa identificadas: {len(threat_model.layer_threats)}")
    print("\nTop riesgos:")
    for t in threat_model.top_risks:
        print(f"  [L{t.layer}] {t.threat_name} -- likelihood={t.likelihood}, impact={t.impact}")
        print(f"    Mitigacion: {t.mitigation}")
    print("\nAmenazas cross-layer:")
    for cl in threat_model.cross_layer_threats:
        print(f"  [capas {cl.layers_involved}] {cl.threat_name} (severity={cl.severity}): {cl.description}")


# ── Sistemas de prueba (los 3 que pide el objetivo del lab) ──────────────
TEST_SYSTEMS = {
    "code_analysis_agent": """
Sistema: Agente de analisis de codigo fuente para seguridad
- Modelo: Gemini via Vertex AI
- Framework: Google ADK v2
- Herramientas: git_clone(), run_sast_scan(), search_nvd_cve(),
                write_jira_ticket(), notify_slack()
- Datos: accede a repos privados de GitHub Enterprise
- Vector store: Pinecone con documentacion de patrones de vulnerabilidades
- Infraestructura: GKE (Google Kubernetes Engine), us-central1
- Multi-agent: orquestador + 3 subagentes especializados (SAST, SCA, secrets)
- Usuarios: equipo de seguridad (20 personas), integracion CI/CD
""",
    "rag_customer_support": """
Sistema: Agente RAG de soporte al cliente
- Modelo: Gemini via Vertex AI
- Framework: Google ADK v2
- Herramientas: search_knowledge_base(), create_support_ticket(), escalate_to_human()
- Datos: base de conocimiento de politicas de la empresa (ChromaDB)
- Vector store: ChromaDB local, embeddings all-MiniLM-L6-v2
- Infraestructura: contenedor Docker en Cloud Run
- Multi-agent: agente unico
- Usuarios: publico general via chat web, sin autenticacion
""",
    "financial_trading_agent": """
Sistema: Agente de trading algoritmico
- Modelo: Gemini via Vertex AI
- Framework: Google ADK v2
- Herramientas: submit_order(), get_market_data(), check_risk_limits()
- Datos: feed de mercado en tiempo real, posiciones internas
- Infraestructura: on-prem, latencia critica
- Multi-agent: agente de estrategia + gateway de riesgo (Z3-verified)
- Usuarios: mesa de trading interna, acceso con credenciales de alto privilegio
""",
}


# ── --selftest: instanciacion + logica de parseo, SIN tocar Vertex AI ────
def _self_test() -> None:
    # Guardrail de defensa en profundidad: --selftest NO debe llamar a
    # Runner.run bajo NINGUNA circunstancia (ni por un bug futuro en este
    # archivo). Se parchea Runner.run para que levante si algo lo invoca
    # durante el selftest -- protege contra el mismo tipo de bug que en la
    # version Ollama causo una invocacion real accidental (ahi, CPU gastada;
    # aca, seria una llamada FACTURADA a Vertex AI).
    from google.adk.runners import Runner as _RunnerClass

    def _forbidden_run(*args, **kwargs):
        raise RuntimeError(
            "GUARDRAIL: Runner.run() fue invocado durante --selftest. "
            "--selftest NUNCA debe tocar Vertex AI -- esto es un bug, no una "
            "corrida real. Abortando antes de disparar una llamada facturada."
        )

    _original_runner_run = _RunnerClass.run
    _RunnerClass.run = _forbidden_run

    try:
        _self_test_body()
    finally:
        _RunnerClass.run = _original_runner_run


def _self_test_body() -> None:
    print("[selftest] --- Instanciacion de agentes (output_schema+tools con Gemini nativo) ---")
    assert maestro_agent.output_schema is MaestroThreatModel
    assert len(maestro_agent.tools) == 2
    assert maestro_agent.model == GEMINI_MODEL
    assert maestro_agent_manual.output_schema is None
    assert len(maestro_agent_manual.tools) == 2
    print("[selftest] OK: ambos agentes se instanciaron sin excepcion.")

    print("\n[selftest] --- _LAYERS_ANALYZED registra arity de analyze_layer (llamada directa) ---")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()
    for n in (1, 3, 7):
        analyze_layer(n, "contexto de prueba")
    assert _LAYERS_ANALYZED == {1, 3, 7}, _LAYERS_ANALYZED
    print(f"[selftest] OK: analyze_layer(1/3/7) registro {sorted(_LAYERS_ANALYZED)} en _LAYERS_ANALYZED "
          "(esta es la senal que build_threat_model imprime tras una corrida real contra Vertex AI para "
          "confirmar si el modelo paso por las 7 capas o corto camino al JSON final).")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()

    print("\n[selftest] --- can_use_output_schema_with_tools con Gemini nativo (sin llamar a la red) ---")
    # Este chequeo instancia un objeto Gemini (no invoca Runner ni dispara
    # ninguna llamada -- resolver el nombre del modelo a un objeto Gemini es
    # pura construccion de objeto Python) y consulta el mismo predicado que
    # usa ADK internamente para decidir si combina output_schema+tools "a la
    # Ollama" (competencia en el mismo request) o si usa el workaround por
    # prompt (set_model_response). Ver el HALLAZGO documentado en el
    # docstring del modulo -- este assert es la verificacion concreta de esa
    # afirmacion, no una suposicion sin chequear.
    try:
        from google.adk.models.google_llm import Gemini
        from google.adk.utils.output_schema_utils import can_use_output_schema_with_tools
        can_combine = can_use_output_schema_with_tools(Gemini(model=GEMINI_MODEL))
        print(f"[selftest] can_use_output_schema_with_tools(Gemini nativo) = {can_combine}")
        assert can_combine is False, (
            "Si esto cambia a True en una version futura de ADK, significa que "
            "Gemini nativo empezo a combinar output_schema+tools en el mismo "
            "request (como ya hace LiteLlm) -- lo cual reintroduciria el mismo "
            "riesgo documentado para Ollama, ahora con Gemini. Revisar el "
            "docstring del modulo si esto cambia."
        )
    except (ImportError, AttributeError) as e:
        print(f"[selftest] AVISO: no se pudo verificar can_use_output_schema_with_tools "
              f"(API interna de ADK puede haber cambiado): {e}. Continuando sin este chequeo.")

    print("\n[selftest] --- Parseo robusto: caso feliz (JSON limpio) ---")
    clean_json = json.dumps({
        "system_name": "Sistema de prueba",
        "layer_decomposition": {
            str(i): {"technology": f"tech-{i}", "threats": ["t1"], "severity": "low"}
            for i in range(1, 8)
        },
        "layer_threats": [{
            "layer": 1, "threat_name": "Prompt injection", "description": "desc",
            "agentic_factor": "non-determinism", "likelihood": "high", "impact": "high",
            "mitigation": "input validation",
        }],
        "cross_layer_threats": [{
            "layers_involved": [1, 3], "threat_name": "chained attack",
            "description": "desc", "severity": "medium",
        }],
        "top_risks": [{
            "layer": 1, "threat_name": "Prompt injection", "description": "desc",
            "agentic_factor": "non-determinism", "likelihood": "high", "impact": "high",
            "mitigation": "input validation",
        }],
    })
    model, reason = try_parse_threat_model(clean_json)
    assert model is not None and model.system_name == "Sistema de prueba", reason
    print(f"[selftest] OK: JSON limpio parseado ({reason}).")

    print("\n[selftest] --- Parseo robusto: JSON envuelto en prosa + fences ---")
    wrapped = f"Aca esta el analisis que pediste:\n\n```json\n{clean_json}\n```\n\nEspero que sirva."
    model, reason = try_parse_threat_model(wrapped)
    assert model is not None and model.system_name == "Sistema de prueba", reason
    print(f"[selftest] OK: JSON envuelto en prosa/fences parseado tras extraccion ({reason}).")

    print("\n[selftest] --- Parseo robusto: JSON truncado (respuesta cortada a mitad) ---")
    truncated = clean_json[: len(clean_json) // 2]
    model, reason = try_parse_threat_model(truncated)
    assert model is None, "un JSON truncado NUNCA deberia parsear correctamente"
    print(f"[selftest] OK: fallo detectado sin excepcion, motivo reportado: {reason!r}")

    print("\n[selftest] --- Parseo robusto: respuesta vacia ---")
    model, reason = try_parse_threat_model("")
    assert model is None
    print(f"[selftest] OK: {reason!r}")

    print("\n[selftest] --- build_threat_model degrada sin excepcion cuando AMBOS intentos fallan ---")
    # Doble de prueba inyectado via el parametro ask_fn (NO via monkeypatch
    # de modulo reimportado -- esa tecnica NO intercepta la funcion real,
    # ver el docstring de build_threat_model) -- nunca toca la red/Vertex AI.
    calls = []

    def _fake_ask_fn(agent, prompt, app_name):
        calls.append(app_name)
        return "esto no es JSON en absoluto"

    model, mode, raw = build_threat_model("sistema de prueba", ask_fn=_fake_ask_fn)
    assert model is None and mode == "failed", (model, mode)
    assert "nativo" in raw and "fallback" in raw
    assert calls == ["maestro-native", "maestro-manual"], calls
    print(f"[selftest] OK: build_threat_model devolvio modo={mode!r} sin excepcion, "
          f"probo ambos intentos ({calls}) usando el doble de prueba (0 llamadas reales a Vertex AI).")

    print("\n[selftest] --- build_threat_model retorna 'native' sin llegar al fallback si el 1er intento ya sirve ---")
    # BUG REAL encontrado al portar este selftest (presente tambien en la
    # version Ollama fuente: --selftest ahi termina con AssertionError, no
    # con exit 0 como afirmaba su .md -- no se toco ese archivo, se corrigio
    # solo aca): el doble de prueba devolvia clean_json sin simular que el
    # agente nativo llamo analyze_layer para las 7 capas, asi que
    # build_threat_model SIEMPRE caia al fallback (layers_ok=False) y el
    # assert `mode2 == "native"` fallaba. Fix: el doble de prueba tambien
    # puebla _LAYERS_ANALYZED, simulando la evidencia de tool-calling que un
    # intento nativo exitoso de verdad dejaria.
    calls2 = []

    def _fake_ask_fn_native_ok(agent, prompt, app_name):
        calls2.append(app_name)
        _LAYERS_ANALYZED.update(range(1, 8))
        return clean_json

    model2, mode2, _ = build_threat_model("sistema de prueba", ask_fn=_fake_ask_fn_native_ok)
    assert model2 is not None and mode2 == "native", (model2, mode2)
    assert calls2 == ["maestro-native"], calls2  # NUNCA debe llamar al fallback si el nativo ya sirvio
    print(f"[selftest] OK: modo={mode2!r}, solo se invoco {calls2} (no se gasto el fallback de mas).")

    print("\n[selftest] OK: Lab 2.1 verificado de punta a punta sin invocar Vertex AI.")


if __name__ == "__main__":
    print("=" * 70)
    print("Lab 2.1 - MAESTRO Automatico (Threat Model Generator)")
    print(f"Gemini ({GEMINI_MODEL}) via Vertex AI / AI Studio")
    print("=" * 70)

    if "--selftest" in sys.argv:
        _self_test()
        sys.exit(0)

    for name, system_description in TEST_SYSTEMS.items():
        print(f"\n{'#' * 70}\n# Sistema: {name}\n{'#' * 70}")
        threat_model, mode, raw_text = build_threat_model(system_description)
        if threat_model is not None:
            print(f"\n[modo usado: {mode}]")
            print_threat_model_report(threat_model)
        else:
            print(f"\n[FALLO CONTROLADO -- modo: {mode}] No se pudo construir un "
                  f"threat model valido para '{name}'. Diagnostico:\n{raw_text[:1000]}")
