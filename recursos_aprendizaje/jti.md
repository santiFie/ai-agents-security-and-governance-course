# El claim `jti`: identificador único de un JWT

> **Por qué este documento.** El Lab 3.2 (`labs/ch03-lab2-ollama/workload_identity_lab32.py`)
> usa el campo `jti` de cada SVID para revocar un token individual sin
> esperar a que expire (Ejercicio 3: un SVID con TTL de una hora, sin
> vencer, se rechaza igual si su `jti` está en una lista de revocación).
> Este documento junta el fundamento de ese mecanismo —qué es `jti`, para
> qué sirve, cómo se genera y qué trade-offs de arquitectura trae— en un
> solo lugar, para no repetirlo en la narración en vivo del lab.

## Qué es

`jti` (**JWT ID**) es un *claim* estándar y opcional definido en la
especificación **RFC 7519** para JSON Web Tokens. Funciona como un
identificador único e irrepetible para un token en particular —no para el
usuario, ni para la sesión: para *ese token específico*.

Esto lo distingue de otros claims estándar como `sub` (a quién representa el
token) o `exp` (cuándo vence): `sub` puede repetirse en muchos tokens del
mismo usuario a lo largo del tiempo, `jti` no debería repetirse nunca entre
dos tokens distintos.

## Casos de uso principales

### Prevención de replay attacks

En flujos mTLS, o cuando los JWT se usan como *assertions* de autenticación
(por ejemplo, `client_credentials` con JWTs firmados), el servidor valida
que un `jti` específico se procese una sola vez. Si un atacante intercepta
la petición y retransmite el mismo token, la verificación falla porque ese
`jti` ya figura como consumido — aunque la firma y el `exp` sigan siendo
válidos.

### Revocación y blacklisting

Los JWT son *stateless* por diseño: toda la información para validarlos
viaja en el propio token, firmado, sin necesidad de consultar una base de
datos. Eso es una ventaja de performance, pero trae un problema práctico:
¿cómo revocás un token *antes* de que llegue su `exp`, si nadie tiene que
consultar nada para validarlo?

La solución con menor huella de estado es: al hacer logout o forzar la
invalidez de una sesión, el servidor guarda **únicamente el `jti` revocado**
—no el payload completo— en una lista negra (por ejemplo, en Redis), con un
TTL ajustado a la fecha de expiración del token (`TTL = exp - now`). Pasado
ese TTL, la entrada se puede borrar sin riesgo: el token ya habría vencido
igual por su propio `exp`.

Es exactamente el mecanismo que reproduce el Lab 3.2: `verify_svid()` recibe
un `revocation_list` (un `set` de `jti` revocados) y rechaza el token si su
`jti` aparece ahí, sin importar que el `exp` todavía no se haya cumplido.

### Auditoría y trazabilidad

En arquitecturas distribuidas o de microservicios, un `jti` presente en los
logs permite correlacionar peticiones: rastrear el ciclo de vida completo de
una sesión específica desde el *API Gateway* hacia los servicios
descendentes (*downstream*), sin ambigüedad sobre a cuál de los muchos
tokens emitidos corresponde cada entrada de log.

## Ejemplo de estructura del payload

Un generador habitual de tokens usa **UUIDv4** para el `jti`, porque
garantiza una probabilidad de colisión prácticamente nula:

```json
{
  "iss": "https://auth.domain.com",
  "sub": "usr_01HXYZ...",
  "aud": "https://api.domain.com",
  "exp": 1772659200,
  "iat": 1772655600,
  "jti": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

El SVID del Lab 3.2 sigue el mismo patrón — `MiniSVIDIssuer.issue_svid()`
genera el `jti` con `str(uuid.uuid4())` en el momento de emitir el token.

## Consideraciones de arquitectura

| Aspecto | Consideración técnica |
|---|---|
| **Generación** | Debe ser criptográficamente impredecible: UUIDv4, ULID o un `nanoid` de alta entropía — nunca un contador incremental, que un atacante podría adivinar o enumerar. |
| **Trade-off de estado** | Mantener un *store* (Redis, Memcached) para consultar el `jti` introduce un punto de estado en un flujo típicamente *stateless*. El costo se acota porque el almacenamiento es temporal (`TTL = exp - now`), no permanente. |
| **Anti-replay efectivo** | Un `jti` por sí solo no previene *replay* a menos que el validador mantenga un caché distribuido o un *store* de corta vida para detectar duplicados dentro de la ventana en que el token sigue siendo válido. |

## Para seguir pensando

1. El Lab 3.2 guarda la `revocation_list` en un `set` de Python en memoria,
   local al proceso. En una arquitectura real con varios agentes y varios
   PDP corriendo en paralelo, ¿qué problema aparece si cada instancia
   mantiene su propia lista de revocación en memoria, sin compartirla?
2. Si el `jti` de un SVID se filtrara (por ejemplo, en un log mal
   configurado) pero el atacante no tuviera la clave privada del emisor,
   ¿alcanzaría ese `jti` solo para forjar un token válido? ¿Qué protege
   realmente la integridad del SVID?
3. Comparar el costo de mantener una lista de revocación por `jti` (Ejercicio
   3 del Lab 3.2) contra simplemente emitir SVIDs con TTL muy cortos (unos
   pocos minutos) y no revocar nunca. ¿En qué escenario cada estrategia es
   preferible?
