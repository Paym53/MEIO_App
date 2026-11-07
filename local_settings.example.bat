@rem ============================================================================
@rem  MEIO worker settings: TEMPLATE. Copy this file to  local_settings.bat  (same folder) and fill in your values.
@rem  local_settings.bat holds your secret token: it is gitignored and must never be committed or shared.
@rem ============================================================================

@rem Python of the Seeker environment
set MEIO_PYTHON=C:\Users\ruben\seeker_env\Scripts\python.exe

@rem API token: a long random secret (at least 32 characters). Make one with:
@rem   C:\Users\ruben\seeker_env\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
set MEIO_TOKEN=paste-your-token-here

@rem Seeker license file
set MEIO_LICENSE_FILE=%~dp0engine\license.sio

@rem Your free ngrok domain (ngrok dashboard, Domains), without https://. Leave empty to run the API on this PC only.
set NGROK_DOMAIN=

@rem The ngrok program: "ngrok" if it is on the PATH, otherwise its full path, e.g. C:\Tools\ngrok.exe
set NGROK_EXE=ngrok
