# ScreenBot - Release Stuck Modifier Keys
# Sends key-up events for Ctrl / Shift / Alt / Windows so Windows stops treating
# every click as a Ctrl+click (new link/window) or Ctrl+drag (duplicate).
# Works even when ScreenBot is not running. No reboot required.

$sig = @'
[DllImport("user32.dll")]
public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, System.UIntPtr dwExtraInfo);
'@

try {
    $kb = Add-Type -MemberDefinition $sig -Name 'ScreenBotKeyboardRelease' -Namespace 'ScreenBot' -PassThru -ErrorAction Stop
} catch {
    # Type can already exist if the script is run twice in one session.
    $kb = [ScreenBot.ScreenBotKeyboardRelease]
}

$KEYUP = 0x0002
$EXTENDED = 0x0001
$keys = @(
    @{ Vk = 0x10; Ext = $false }, # Shift
    @{ Vk = 0x11; Ext = $false }, # Ctrl
    @{ Vk = 0x12; Ext = $false }, # Alt
    @{ Vk = 0xA0; Ext = $false }, # Left Shift
    @{ Vk = 0xA1; Ext = $false }, # Right Shift
    @{ Vk = 0xA2; Ext = $false }, # Left Ctrl
    @{ Vk = 0xA3; Ext = $false }, # Right Ctrl
    @{ Vk = 0xA4; Ext = $false }, # Left Alt
    @{ Vk = 0xA5; Ext = $false }, # Right Alt
    @{ Vk = 0x5B; Ext = $true  }, # Left Windows
    @{ Vk = 0x5C; Ext = $true  }  # Right Windows
)

for ($pass = 0; $pass -lt 2; $pass++) {
    foreach ($key in $keys) {
        $flags = $KEYUP
        if ($key.Ext) { $flags = $flags -bor $EXTENDED }
        try {
            $kb::keybd_event([byte]$key.Vk, 0, [uint32]$flags, [System.UIntPtr]::Zero)
        } catch { }
    }
    Start-Sleep -Milliseconds 80
}

Write-Host "[OK] Modifier key state reset (Ctrl/Shift/Alt/Windows)."
Write-Host "Normal clicks should work again. If not, run this script once more."
