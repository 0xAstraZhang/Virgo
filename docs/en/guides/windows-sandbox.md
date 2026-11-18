# Windows command sandbox

The native x64 Windows backend requires PowerShell 7, local NTFS and the rights needed to create its AppContainer profile and temporary ACLs. Virgo checks the suspended process's LPAC token and Job membership before release. The Job must drain before the broker revokes the temporary grants. Windows retains ambient LPAC-readable resources.

Workspace-Edit grants the command the Internet and private-network capabilities without a Virgo approval prompt. Read-Only omits these capabilities until an exact operation is approved. The host firewall and AppContainer network policy control actual outbound, inbound and host-loopback connectivity. Virgo leaves firewall rules and loopback exemptions under the user's control.

Per-operation filesystem access uses the exact existing object for a file and the specified tree for a directory. An unavailable path or an ACL that cannot be installed refuses the operation. The broker does not request elevation.

At `37d4427`, Windows Server 2022 and 2025 administrator runners established external TCP connections. Host loopback and host-interface connections were unsuccessful. The inbound socket bound and listened, but the host connection timed out. [Windows native reports](https://github.com/JunhangYU1017/Virgo/actions/runs/44270022381) · [Acceptance procedure](../development/execution-acceptance.md)

A positive live-reader test on Windows 11 build 26200, native x64 Python 3.13.9 and a non-elevated desktop token stopped before the payload. Creating the protected KsecDD device coordination mutex returned Windows error 1307. The existing LPAC implementation requires that lease. Egress, ingress and loopback were unmeasured on this account.
