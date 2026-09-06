$configs = @(
    "pipeline/config/experiments/stage2_gemini_flash_8b.yaml",
    "pipeline/config/experiments/stage2_gemini_flash.yaml",
    "pipeline/config/experiments/stage2_gemini_pro.yaml",
    "pipeline/config/experiments/stage2_vision_only.yaml"
)

foreach ($config in $configs) {
    Write-Host "=================================================="
    Write-Host "Running harness with config: $config"
    Write-Host "=================================================="
    
    uv run eval/harness.py --config $config
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: Harness returned non-zero exit code for $config" -ForegroundColor Yellow
    }
}

Write-Host "=================================================="
Write-Host "All experiments complete!"
Write-Host "=================================================="
