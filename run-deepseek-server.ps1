$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

Write-Host "DeepSeek 前端测试模式"
Write-Host "请使用重新生成且未在聊天中公开的新密钥。"
$SecureKey = Read-Host "请输入新密钥（输入不会显示，也不会写入文件）" -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)

try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    $env:USE_MOCK_LLM = "false"
    & .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
}
finally {
    Remove-Item Env:\DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\USE_MOCK_LLM -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}

