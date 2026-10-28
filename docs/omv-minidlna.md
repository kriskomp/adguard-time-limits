# MiniDLNA on OpenMediaVault – time-limited

netguard can switch the MiniDLNA media server of an OpenMediaVault (OMV) box on for a number of granted minutes
and off again when the time is up. It works like a device profile: **+15 / +30 / +60** enables DLNA, **-15** or
**Blokuj** (block) and the end of the time disable it.

## How it works

```
netguard (LXC) ──ssh, key restricted to one command──▶ OMV: /opt/sh/dlna.sh status|start|stop
                                                           │ omv-rpc MiniDlna setSettings   (the "Enabled" switch)
                                                           │ omv-rpc Config applyChanges    (module minidlna, like "Apply")
                                                           └ waits until minidlna.service is active/inactive
```

- The OMV configuration is changed through the OMV API, so the OMV web UI always shows the real state and
  never asks you to "apply pending changes". Only the *Enabled* field is touched, all other MiniDLNA settings stay.
- The SSH key can run **only** `dlna.sh` (forced command + `restrict`), and the script accepts only
  `status`, `start` and `stop`. Anything else is refused.
- The script is idempotent: if the state already matches it does nothing (no needless deploy).
- Make sure DLNA is **disabled in OMV** (Services → DLNA → untick *Enabled* → Save → Apply) so it is off by default
  after an OMV restart. netguard also enforces the state at its own start.

## Setup (once)

### 1. On OMV – install the script

Copy `scripts/omv/dlna.sh` from this repository to `/opt/sh/dlna.sh` on the OMV host and make it executable:

```bash
mkdir -p /opt/sh && cp dlna.sh /opt/sh/dlna.sh && chmod 750 /opt/sh/dlna.sh && /opt/sh/dlna.sh status
```

`status` must print something like `enabled=false active=inactive`.

### 2. On the netguard host – install ssh and create a key

```bash
apt install -y openssh-client && ssh-keygen -t ed25519 -N '' -C netguard-omv -f /etc/netguard/omv_key && chown netguard:netguard /etc/netguard/omv_key /etc/netguard/omv_key.pub && chmod 600 /etc/netguard/omv_key && cat /etc/netguard/omv_key.pub
```

The private key must belong to `netguard` with mode `600` (ssh refuses anything looser).

### 3. On OMV – authorize the key for the script only

Add one line to `/root/.ssh/authorized_keys` (replace `AAAA…` with the public key printed above):

```
command="/opt/sh/dlna.sh",restrict ssh-ed25519 AAAA... netguard-omv
```

Root login with a key must be allowed (default Debian `PermitRootLogin prohibit-password`) and the SSH service
must be enabled in OMV. If you prefer a non-root user, it needs permission to run `omv-rpc` – root is the simple route.

### 4. On the netguard host – configure and restart

In `/etc/netguard/netguard.env` set:

```
OMV_HOST=<OMV address>
OMV_SSH_USER=root
OMV_SSH_KEY=/etc/netguard/omv_key
OMV_KNOWN_HOSTS=/opt/netguard/data/known_hosts
```

Accept the host key once (as the service user) and test the whole chain:

```bash
runuser -u netguard -- ssh -i /etc/netguard/omv_key -o UserKnownHostsFile=/opt/netguard/data/known_hosts -o StrictHostKeyChecking=accept-new root@<OMV address> status
```

Expected: `enabled=false active=inactive`. Then `systemctl restart netguard`.

### 5. In the panel

Choose the profile type **MiniDLNA na OMV**, give it a name (e.g. `DLNA`) and save. A new profile is blocked
(DLNA off). Add minutes → DLNA turns on within a few seconds; the table shows the state reported by OMV.

## Troubleshooting

| Symptom | Check |
|---|---|
| Panel shows `OMV nieskonfigurowany` | `OMV_HOST` / `OMV_SSH_KEY` in `/etc/netguard/netguard.env`, restart |
| `Permission denied (publickey)` | key line in OMV `authorized_keys`, key file owner/mode (600, owner `netguard`) |
| `Host key verification failed` | run the one-time `ssh … status` command from step 4 as `netguard` |
| `brak polecenia ssh` | `apt install openssh-client` |
| Error `kod=2 … getSettings/applyChanges` | run `/opt/sh/dlna.sh status` on OMV and read the message; OMV has to be healthy |
| Error `kod=3` | the service did not reach the wanted state within 90 s: `journalctl -u minidlna` on OMV |
| OMV panel shows a yellow "pending changes" bar | another change is waiting in OMV; apply it there – netguard's own change never leaves one |

History (panel, or `GET /events`) lists `service_on`, `service_off` and `service_FAIL` for the profile.
