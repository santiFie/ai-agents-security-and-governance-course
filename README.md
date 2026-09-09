# Seminario Seguridad en Agentes de IA — UNLP 2026 (material para alumnos)

Curso de posgrado sobre seguridad en sistemas de IA agéntica. Este repo es un subconjunto
curado de materiales pensado específicamente para alumnos, con lo publicado hasta el momento:
**Capítulos 1, 2 y 3**, un ejercicio de red-teaming educativo contra una plataforma
pública, módulos de nivelación por prerrequisito, recursos de aprendizaje adicionales, y las
slides del curso en PDF.

## Los 2 sitios en vivo del último encuentro

Estos dos sitios públicos son los que se recorrieron en la última clase — vale la pena
abrirlos y explorarlos por tu cuenta:

- **[MAESTRO Sentinel](https://maestro-sentinel.com/MAESTROEducation)** — módulo educativo
  interactivo del framework MAESTRO (las 7 capas de superficie de ataque de un agente), base
  teórica de todo el Capítulo 2.
- **[Lakera Agent Breaker](https://play.lakera.ai/agent-breaker)** — catálogo público de 10
  apps con agentes de IA deliberadamente vulnerables, para practicar prompt injection, tool
  poisoning, memory poisoning y jailbreaks. Es la plataforma contra la que corre el ejercicio
  documentado en `lakera/`.

## Índice — qué leer para estar al día

Orden recomendado de lectura por capítulo. Los **labs en código son opcionales pero
recomendados**: profundizan lo mismo que ya se explica en las guías y en la teoría, corriendo
el ataque o el mecanismo de verdad en vez de solo leerlo.

### Capítulo 1 — Anatomía de un agente y superficie de ataque

1. Teoría: `slides/chapter01/teoria.pdf`
2. Introducción a los labs: `slides/chapter01/intro-labs.pdf`
3. *(opcional, recomendado)* Labs en código — guía de cada uno en `labs/guias_alumnos/chapter01/`,
   código ejecutable en `labs/chapter01/lab{1,2,3}-*/lab{1,2,3}-{ollama,gcp}/`:
   - Lab 1.1 — Anatomía de un agente (`lab1-anatomia-agente.md`)
   - Lab 1.2 — Confused Deputy: demostración y mitigación (`lab2-confused-deputy.md`)
   - Lab 1.3 — Mapeo de superficie de ataque (`lab3-superficie-ataque.md`)

### Capítulo 2 — Framework MAESTRO y ataques a agentes

1. Sitio interactivo **MAESTRO Sentinel** (arriba) — recorrerlo es el punto de partida de
   este capítulo.
2. Teoría: `slides/chapter02/teoria.pdf`
3. Introducción a los labs: `slides/chapter02/intro-labs.pdf`
4. *(opcional, recomendado)* Labs en código — guía de cada uno en `labs/guias_alumnos/chapter02/`,
   código ejecutable en `labs/chapter02/lab{1,2,3}-*/lab{1,2,3}-{ollama,gcp}/`:
   - Lab 2.1 — Generador automático de threat models MAESTRO (`lab1-maestro-threat-model.md`)
   - Lab 2.2 — RAG Poisoning (`lab2-rag-poisoning.md`)
   - Lab 2.3 — Observabilidad estructurada por capa MAESTRO (`lab3-maestro-observability.md`)
5. Ejercicio de red-teaming contra **Lakera Agent Breaker** (arriba) — carpeta `lakera/`,
   empezar por `lakera/hack-llm-lakera.md` (índice general, con mapeo a OWASP LLM Top 10 /
   OWASP Agentic Top 10 / MITRE ATLAS) o por el resumen ejecutivo en PDF.

### Capítulo 3 — Identidad y Autenticación de Agentes de IA

1. Teoría: `slides/chapter03/teoria.pdf`
2. Anexos: `slides/chapter03/anexo-0-agent-engine-runtime.pdf`,
   `slides/chapter03/anexo-agent-identity-nativo.pdf`,
   `slides/chapter03/anexo-opa-rego-agent-gateway.pdf` y
   `slides/chapter03/anexo-gcp-agent-platform.pdf`
3. Caso de estudio: `slides/chapter03/operacion-encubierta.pdf`
4. *(opcional, recomendado)* Labs en código — guía de demo en
   `labs/guias_alumnos/chapter03/guia-demo-alumnos.md`, código ejecutable en
   `labs/chapter03/lab{1,3,4}-*/lab{1,3,4}-ollama/`:
   - Lab 3.1 — OPA Policy Engine: autorización ABAC para agentes (`lab1-opa-policy-engine`)
   - Lab 3.3 — Secretless Architecture (GCP IAM) (`lab3-secretless`)
   - Lab 3.4 — NeMo Guardrails: política conversacional declarativa (`lab4-nemo-guardrails`)
   - *(El Lab 3.2 — Workload Identity y las variantes `-gcp` de los labs 3.1/3.3/3.4 aún no
     fueron publicados en el repo.)*

## Recursos complementarios (autoestudio, no ligados a un capítulo puntual)

- **`nivelacion/`** — Módulos de nivelación por prerrequisito, para repasar antes de o en
  paralelo con el curso: setup de entorno, Python intermedio, fundamentos de LLMs/agentes,
  autenticación JWT/PKI, ML/deep learning, policy-as-code (Rego/OPA), lógica formal/SAT
  solving, estadística inferencial. Empezar por `nivelacion/README.md`.
- **`recursos_aprendizaje/`** — Profundizaciones y links externos verificados sobre temas
  transversales del curso: el gradiente y las técnicas de fine-tuning (SFT/LoRA/RLHF)
  explicados en detalle, *safety alignment* y cómo se entrena con RLHF/PPO, Privacidad
  Diferencial aplicada a seguridad agéntica, ataques adversariales de evasión (FGSM/PGD/GCG),
  embeddings y búsqueda vectorial en RAG, explicabilidad (SHAP/LIME), una nota práctica sobre
  correr modelos chicos en CPU, y una lista de repos open-source de seguridad de IA.
- **`slides/`** — Además de las slides de Cap. 1, 2 y 3 ya listadas arriba: la apertura general
  del seminario (`slides/chapter00/intro-seminario-unlp.pdf`) y el panorama de modelos de
  frontera (`slides/chapter00/modelos-frontera.pdf`).

## Cómo correr los labs (código opcional)

```bash
pip install -r requirements.txt
```

Para las variantes `-ollama`: instalar [Ollama](https://ollama.com) y descargar el modelo local
con `ollama pull qwen3.5:9b` antes de correr cualquier script.

Para las variantes `-gcp`: necesitás un proyecto de Google Cloud con Vertex AI habilitado y
credenciales configuradas (`gcloud auth application-default login`), o una API key de Gemini vía
AI Studio.

Cada script tiene un modo `--selftest` que verifica la lógica del lab sin invocar ningún modelo —
es el punto de partida recomendado antes de correr el lab completo.

## Sobre el curso

Curso de posgrado sobre seguridad en sistemas de IA agéntica. Audiencia: programadores,
científicos de datos e ingenieros con Python, ML/LLMs a nivel de uso, y conceptos básicos de
seguridad.

Este repo cubre los Capítulos 1, 2 y 3 del curso completo (del Cap. 3 están publicadas las
slides y los labs 3.1, 3.3 y 3.4 en variante `-ollama`; el Lab 3.2 y las variantes `-gcp`
aún no fueron publicados). Si estás cursando el seminario, tu docente te va a compartir el
resto del material a medida que avanza el curso.
