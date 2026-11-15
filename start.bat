@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo [エラー] uv コマンドが見つかりません。uv をインストールして PATH を通してください。
    echo https://docs.astral.sh/uv/
    pause
    exit /b 1
)

if not exist ".env" (
    echo [エラー] .env ファイルが見つかりません。
    echo .env.example をコピーして .env を作成し、DISCORD_TOKEN を設定してください。
    pause
    exit /b 1
)

echo yajubot を起動しています...
uv run yajubot
set "EXIT_CODE=%errorlevel%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [エラー] ボットが異常終了しました (終了コード: %EXIT_CODE%^)。
    pause
    exit /b %EXIT_CODE%
)

endlocal
