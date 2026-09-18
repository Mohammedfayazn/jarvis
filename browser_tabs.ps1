# Ek browser window ke tabs - Windows UI Automation se.
#
# browser_tools.py isko chalata hai. Python me isliye nahi ki UI Automation
# .NET me Windows ke saath muft aata hai; Python se chahiye toh nayi
# dependency (comtypes/pywinauto) lagti.
#
#   -Mode list   har tab: id, title, selected - JSON me
#   -Mode close  stdin par {"close": [ids], "keep": id} - woh tabs band,
#                phir "keep" wale tab par wapas
#
# Tab band karna = pehle tab select, phir uska apna Close button. Keystrokes
# (Ctrl+W) jaan-boojh kar nahi: focus beech me hila toh Ctrl+W kisi aur app
# me ja girta. Aur select isliye, kyunki bahut tabs hon toh Chrome sirf
# saamne wale tab par Close button dikhata hai.

param(
    [Parameter(Mandatory = $true)][Int64]$Hwnd,
    [Parameter(Mandatory = $true)][ValidateSet('list', 'close')][string]$Mode
)

$ErrorActionPreference = 'Stop'
# Titles me Hindi, emoji sab aata hai - PowerShell 5.1 warna OEM codepage
# me bigaad deta hai
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$AE = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$Scope = [System.Windows.Automation.TreeScope]
$SelectPattern = [System.Windows.Automation.SelectionItemPattern]::Pattern
$InvokePattern = [System.Windows.Automation.InvokePattern]::Pattern

$TabCondition = New-Object System.Windows.Automation.PropertyCondition(
    $AE::ControlTypeProperty, $CT::TabItem)
$ButtonCondition = New-Object System.Windows.Automation.PropertyCondition(
    $AE::ControlTypeProperty, $CT::Button)

function Get-Tabs($root) {
    @($root.FindAll($Scope::Descendants, $TabCondition))
}

# RuntimeId element ke rehte tak nahi badalta - title badal jaye (YouTube
# ka "(135)" counter) toh bhi wahi tab pakda jata hai
function Get-TabId($element) {
    ($element.GetRuntimeId() -join '.')
}

function Test-Selected($element) {
    try { [bool]$element.GetCurrentPattern($SelectPattern).Current.IsSelected }
    catch { $false }
}

function Get-CloseButton($tab) {
    $buttons = @($tab.FindAll($Scope::Descendants, $ButtonCondition))
    if ($buttons.Count -eq 0) { return $null }   # pinned tab - band hota hi nahi
    # Angrezi Chrome me "Close"; doosri bhasha me naam alag, tab sabse
    # daayan wala button - close hamesha wahi hota hai
    $named = $buttons | Where-Object { $_.Current.Name -match '^close' } | Select-Object -First 1
    if ($named) { return $named }
    $buttons[-1]
}

$root = $AE::FromHandle([IntPtr]$Hwnd)

if ($Mode -eq 'list') {
    $tabs = foreach ($tab in Get-Tabs $root) {
        [pscustomobject]@{
            id       = Get-TabId $tab
            title    = $tab.Current.Name
            selected = Test-Selected $tab
        }
    }
    ConvertTo-Json -InputObject @($tabs) -Compress
    exit 0
}

# --- close ---

$request = [Console]::In.ReadToEnd() | ConvertFrom-Json

# Ek hi baar dhoondo - har band ke baad poora tab strip dobara khangaalna
# 20+ tabs par dheema padta hai. Elements zinda rehte hain; jo tab ja chuka,
# uspar call ElementNotAvailable phenkta hai aur hum aage badh jate hain.
$byId = @{}
foreach ($tab in Get-Tabs $root) { $byId[(Get-TabId $tab)] = $tab }

foreach ($id in @($request.close)) {
    $tab = $byId[$id]
    if (-not $tab) { continue }
    try {
        $tab.GetCurrentPattern($SelectPattern).Select()
        Start-Sleep -Milliseconds 150   # Close button select ke baad ubharta hai
        $close = Get-CloseButton $tab
        if ($close) {
            $close.GetCurrentPattern($InvokePattern).Invoke()
            Start-Sleep -Milliseconds 120
        }
    }
    catch { }   # Kya band hua, woh Python baad me list se khud ginta hai
}

# Kaam wale tab par wapas - band karte waqt selection idhar-udhar hua tha
if ($request.keep -and $byId.ContainsKey($request.keep)) {
    try { $byId[$request.keep].GetCurrentPattern($SelectPattern).Select() } catch { }
}

ConvertTo-Json -InputObject ([pscustomobject]@{ done = $true }) -Compress
