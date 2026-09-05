# Android capability matrix

| Capability | Private alpha | Play flavor | Hard platform boundary |
|---|---:|---:|---|
| Calendar | Yes | Yes | Only authorized calendars and granted fields |
| UsageStats | Yes | Yes | User must enable special access; OEMs may delay data |
| Notifications | Full local body | Metadata only | Apps may redact content; historical notifications unavailable |
| Location | Explicit opt-in | Explicit opt-in | Background access and OEM throttling apply |
| Health Connect | Optional | Optional | Per-record permissions; source coverage varies |
| Accessibility | Optional | No | Secure/password nodes and protected app content remain hidden |
| Microphone | Visible session | No | Foreground indicator/service required; no covert recording |
| Screen capture | Per-session | No | User approval every session; secure windows render blank |
| Custom IME | Optional | No | Sees only text typed through this IME; password fields excluded |
| Email | OAuth2 or app password | OAuth2 | Provider policy, rate, retention, and account controls apply |
| WeChat | Notification/visible UI/import | Notification metadata | Private database is sandboxed and encrypted |
| External actions | Confirmed adapters | Confirmed adapters | Provider/API permissions and user confirmation apply |
| Local 7B | Device-dependent | Device-dependent | RAM, storage, thermals, runtime and model license apply |

All collectors have an independent consent, pause, retention, and deletion
switch. Friends own their keys and cannot be inspected from an administrator
account.
