# Fixed program; all notification data arrives as JSON on stdin, never as code.
$ErrorActionPreference = 'Stop'
try {
    $data = [Console]::In.ReadToEnd() | ConvertFrom-Json
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.UI.Notifications.ToastNotifier, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.UI.Notifications.NotificationSetting, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
    # Windows PowerShell's existing Start Menu shortcut supplies a registered
    # desktop identity. No app installation, registry edits or admin rights.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
    if ($null -ne $notifier.Setting -and $notifier.Setting.ToString() -ne 'Enabled') { throw 'Notifications disabled' }
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
    $nodes = $xml.GetElementsByTagName('text')
    $null = $nodes.Item(0).AppendChild($xml.CreateTextNode([string]$data.title))
    $null = $nodes.Item(1).AppendChild($xml.CreateTextNode([string]$data.body))
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $toast.Tag = [string]$data.tag
    $toast.Group = 'Paveway'
    $toast.ExpirationTime = [DateTimeOffset]::Now.AddMinutes(5)
    $notifier.Show($toast)
    [Console]::Out.WriteLine('PAVEWAY_NOTIFICATION_ACCEPTED')
} catch {
    [Console]::Error.WriteLine('PAVEWAY_WINDOWS_NOTIFICATION_UNAVAILABLE')
    exit 2
}
