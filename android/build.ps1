param(
    [string]$Sdk = $(if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA 'Android\Sdk' }),
    [string]$Keystore = $(Join-Path $env:USERPROFILE '.android\debug.keystore')
)
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$tools = Join-Path $Sdk 'build-tools\35.0.0'
$platform = Join-Path $Sdk 'platforms\android-34\android.jar'
$work = Join-Path ([IO.Path]::GetTempPath()) ('keepeda-android-' + [guid]::NewGuid().ToString('N'))
$classes = New-Item -ItemType Directory -Force -Path (Join-Path $work 'classes')
$dex = New-Item -ItemType Directory -Force -Path (Join-Path $work 'dex')
function Run-Tool($executable, [string[]]$arguments) {
    & $executable @arguments
    if ($LASTEXITCODE -ne 0) { throw "$executable failed ($LASTEXITCODE)" }
}
if (-not (Test-Path -LiteralPath $Keystore)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Keystore) | Out-Null
    Run-Tool 'keytool' @('-genkeypair','-keystore',$Keystore,'-storepass','android','-keypass','android',
        '-alias','androiddebugkey','-keyalg','RSA','-keysize','2048','-validity','10000','-dname','CN=Android Debug')
}
$apk = Join-Path $work 'unsigned.apk'
Run-Tool (Join-Path $tools 'aapt2.exe') @('link','-I',$platform,'--min-sdk-version','34','--target-sdk-version','34',
    '--manifest',(Join-Path $source 'AndroidManifest.xml'),'-o',$apk)
$sources = @(Get-ChildItem -LiteralPath $source -Filter '*.java' -File | ForEach-Object FullName)
Run-Tool 'javac' (@('--release','8','-encoding','UTF-8','-classpath',$platform,'-d',$classes.FullName) + $sources)
$bytecode = @(Get-ChildItem -LiteralPath $classes.FullName -Filter '*.class' -File -Recurse | ForEach-Object FullName)
Run-Tool (Join-Path $tools 'd8.bat') (@('--min-api','34','--lib',$platform,'--output',$dex.FullName) + $bytecode)
Run-Tool 'jar' @('uf',$apk,'-C',$dex.FullName,'classes.dex')
$aligned = Join-Path $work 'aligned.apk'
$signed = Join-Path $work 'keepeda-sensors.apk'
Run-Tool (Join-Path $tools 'zipalign.exe') @('-f','4',$apk,$aligned)
Run-Tool (Join-Path $tools 'apksigner.bat') @('sign','--ks',$Keystore,'--ks-pass','pass:android','--out',$signed,$aligned)
Run-Tool (Join-Path $tools 'apksigner.bat') @('verify',$signed)
Copy-Item -LiteralPath $signed -Destination (Join-Path (Split-Path $source) 'data\keepeda-sensors.apk') -Force
