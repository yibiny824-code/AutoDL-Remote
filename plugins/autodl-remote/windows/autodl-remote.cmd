@echo off
setlocal
if defined AUTODL_REMOTE_WINDOWS_PYTHON (
  "%AUTODL_REMOTE_WINDOWS_PYTHON%" "%~dp0controller.py" %*
) else (
  python "%~dp0controller.py" %*
)
exit /b %errorlevel%
