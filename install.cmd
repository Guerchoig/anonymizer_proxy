@echo off
rem ============================================================
rem  Anonymizer Proxy - Windows installer (wrapper for install.ps1)
rem
rem  Run THIS file (double-click), not install.ps1 directly:
rem  1) it removes the Mark-of-the-Web flag that Windows adds to
rem     files extracted from a downloaded ZIP (otherwise the
rem     RemoteSigned policy demands a digital signature);
rem  2) it invokes PowerShell with -ExecutionPolicy Bypass, so the
rem     execution policy does not block the installation.
rem
rem  NOTE: keep this file ASCII-only. cmd.exe parses batch files
rem  in the legacy OEM codepage, and UTF-8 Cyrillic text here
rem  breaks parsing on Russian Windows.
rem ============================================================
cd /d "%~dp0"

echo Removing "downloaded from internet" block flags (Unblock-File)...
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '.' | Unblock-File" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 goto :error
goto :done

:error
echo.
echo [ERROR] Installation failed. Common causes:
echo.
echo  1. Execution policy enforced by Group Policy (AllSigned):
echo     the error text contains "cannot be overridden ... on this
echo     computer". Ask your IT department, or run in PowerShell
echo     as administrator:
echo        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
echo        .\install.ps1
echo.
echo  2. No internet access for downloading uv / Python / models.
echo.
pause
exit /b 1

:done
pause
