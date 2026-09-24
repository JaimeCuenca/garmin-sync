# BluePulse — Garmin sync

Repo (puede ser público) que solo contiene el script de extracción y su
GitHub Action. No guarda datos personales: los escribe en el repo privado
`bluepulse-data` vía la API de GitHub.

## Configuración

1. Crea este repo en GitHub y sube esta carpeta.
2. En Settings -> Secrets and variables -> Actions, añade:
   - `GARMIN_EMAIL`, `GARMIN_PASSWORD` -> tus credenciales de Garmin Connect
   - `DATA_REPO` -> `tu_usuario/bluepulse-data`
   - `DATA_REPO_TOKEN` -> un GitHub Personal Access Token (fine-grained) con
     permiso `contents: write` ÚNICAMENTE sobre el repo `bluepulse-data`
3. La Action corre sola cada día a las 05:30 UTC. También puedes lanzarla a
   mano desde la pestaña "Actions" -> "Sync Garmin data" -> "Run workflow".

## Cómo funciona la sincronización incremental

- Guarda `garmin/last_sync.json` en el repo de datos con la última fecha
  sincronizada.
- Cada ejecución sincroniza desde esa fecha hasta hoy (así si un día falla
  la Action, el día siguiente se recupera el hueco).
- Si nunca se ha ejecutado, sincroniza los últimos `SYNC_DAYS_BACK` días
  (7 por defecto, configurable como variable de entorno/secret).

## Nota sobre `python-garminconnect`

Es una librería NO oficial que hace scraping/reversing de la API interna de
Garmin Connect. Garmin puede cambiar su API sin avisar y romper esto en
cualquier momento — si un día la Action falla, lo normal es que haya que
actualizar la librería (`pip install -U garminconnect`) o revisar si hay un
cambio de flujo de login (a veces añaden MFA, captchas, etc.).
