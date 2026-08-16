# drive-bays — physical drive-bay map

A chassis bay map for the Nexus Dashboard: every drive shown in the bay it
physically occupies, with identity, usage and SMART, plus real identify/fault
LED control. This is a from-scratch replacement for the 45Drives **Houston
"45Drives Disks"** plugin — same capability, no dependency on anything
45Drives ships.

Python tier plugin. Drop-in: it touches no core dashboard file.

## How the bay mapping works

The kernel's **SCSI Enclosure Services** driver exposes every backplane slot
under `/sys/class/enclosure/<enclosure>/<component>/`, with `slot`, `status`,
`device/block/<name>`, and writable `locate` / `fault`. That is where the map
comes from — it is generic kernel infrastructure, not vendor tooling, so it
works on **any** SES-capable backplane or HBA, on any distribution.

SES reports one thing it cannot know: **physical geometry**. It hands out an
ordinal slot number, never a position. Bay labels are therefore resolved in
three tiers, and every bay reports which tier it used (`label_source`):

| Tier | Source | Used when |
|---|---|---|
| 1 | `/etc/vdev_id.conf` | 45Drives' `dmap` has written one — these are the authoritative chassis labels |
| 2 | chassis profile in `plugin.py` | `Chassis Size` is known from `server_info.json` |
| 3 | raw SES slot number | nothing else is available |

Where tiers 1 and 2 disagree for a drive, the page shows a warning and
displays the `vdev_id.conf` label. Neither optional file is required.

### The padding-slot problem

A backplane commonly advertises more array-device slots than the chassis has
bays. On an HL15 the enclosure reports **48 components**: 32 are `unsupported`
(dropped outright), leaving 16 real slots for a **15-bay** chassis. The
chassis profile's `bays` count trims the surplus from the end. The dropped
slots stay in the API response as `trimmed_slots` for debugging, but are
deliberately NOT shown on the page: every backplane over-advertises, so a
notice about it would be permanent, normal and ignored.

Adding a chassis is one line in `_CHASSIS_PROFILES` (bay count, row count,
label format). With no profile at all the plugin still works, showing every
real slot by number.

### Disks that are not in a bay

Onboard M.2, SATA and USB devices have no enclosure component and no carrier
group, so they get **no position at all** — they are listed separately under
"Not in a bay". Inventing one would misrepresent the hardware. (Drives on a
bifurcated M.2 carrier DO get a position; see below.)

## Install

```sh
sudo cp -r drive-bays /opt/nexus-dashboard/plugins/      # or your app dir
sudo chown -R root:root /opt/nexus-dashboard/plugins/drive-bays
sudo chmod -R go-w      /opt/nexus-dashboard/plugins/drive-bays
sudo systemctl restart nexus-dashboard                   # or your unit name
```

The loader refuses world-writable plugin files, hence the `chmod`. On a fleet
node keeping legacy names the app dir is `/opt/storage-dashboard` and the unit
is `storage-dashboard`.

Then enable **Drive Bays** on the Modules page — plugins always start
disabled.

## Optional: identify / fault LEDs

Reading the bay map needs **no privilege** — sysfs is world-readable, and
`lsblk` runs unprivileged. Only LED *writes* need root, through this plugin's
own helper:

```sh
sudo install -o root -g root -m 0755 \
    /opt/nexus-dashboard/plugins/drive-bays/helper/nexus-bay-led \
    /usr/local/sbin/nexus-bay-led
```

Grant the dashboard's service user exactly that helper. Use the **two-line
pattern**: a lone trailing `*` matches any remaining arguments on both classic
sudo (Rocky) and sudo-rs (Ubuntu 26.04+), whereas a wildcard embedded inside
an argument word is rejected by sudo-rs.

```
# /etc/sudoers.d/nexus-dashboard-drive-bays   (mode 0440)
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-bay-led
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-bay-led *
```

**Always validate before installing** — both sudo flavours error-recover past
a bad file, so breakage is silent:

```sh
sudo visudo -cf /etc/sudoers.d/nexus-dashboard-drive-bays
```

Without the helper the page works normally and the LED buttons are replaced by
a note. SMART additionally uses the dashboard's existing `smartctl` grant.

The helper is a root trust boundary: it re-validates every argument itself
(numeric enclosure id and component, attribute strictly `locate` or `fault`,
value strictly `0` or `1`), so no argument can traverse out of
`/sys/class/enclosure`.

## M.2 carrier cards (bifurcated PCIe adapters)

Drives on a passive M.2 carrier (ASUS Hyper M.2 and similar) get a position
too, from a completely different signal than the bay map.

The card itself is **undetectable and is never named**: it has no PCIe switch,
no controller and no PCI ID, so nothing on it enumerates. What bifurcation
does leave behind is structure — the motherboard splits one x16 slot into four
x4 root ports at consecutive **functions** of the same PCI device, and each
drive hangs off one of them. So:

| Fact | Source |
|---|---|
| Which card a drive is on | its endpoint's parent root-port device (`0000:00:01`) |
| Position on the card | the root port's function number (`.1` → socket 1) |
| Which motherboard slot | SMBIOS type 9, matched on the first function's bus address |

A group of one is not a carrier — a lone NVMe is just a device in a slot, and
is listed under "Not in a bay" instead.

**Socket numbering runs in lane order, which is stable and monotonic, but
which physical end is socket 1 is a card-layout convention nothing reports.**
Confirm it once against your hardware and it holds thereafter.

Optional: slot names (`PCIE5` instead of `PCIe group 0000:00:01`) need one
exact-pin grant. Same two-line, sudo-rs-safe pattern; the second line covers
distros where `/sbin` is not a symlink:

```
# /etc/sudoers.d/<unit-prefix>-drive-bays-dmi   (mode 0440)
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/dmidecode -t slot
dashboard ALL=(ALL) NOPASSWD: /sbin/dmidecode -t slot
```

Without it the carriers still render, labelled by PCI group.

## API

| Route | Purpose |
|---|---|
| `GET /api/drive-bays` | chassis, enclosures, bays, M.2 carriers, and disks in neither |
| `GET /api/drive-bays/health` | SMART for every disk in a bay (`?all=1` for the rest) |
| `POST /api/drive-bays/led` | set a bay's `locate`/`fault` LED (admin) |

SMART is deliberately a separate call — it runs `smartctl` per drive and a
full chassis can hold 60, so the map paints first and health fills in on
request.

## Requirements & portability

- An SES-capable backplane or HBA. On a 45Drives HL15 the enclosure is
  synthesized by the **Broadcom HBA** (`BROADCOM VirtualSES`), not the
  backplane, so it appears on any host with an LSI/Broadcom tri-mode HBA.
  Plain AHCI/NVMe exposes nothing, and the page says so cleanly.
- `lsblk` (util-linux) and `smartctl` (smartmontools) — present on both
  Rocky 9 and Ubuntu.
- Standard library only. Valid on **Python 3.9 (Rocky 9) through 3.14
  (Ubuntu 26.04)**: no match statements, no PEP 604 unions, no walrus in
  comprehensions.
- No 45Drives package is required at any tier.

## Degradation

| Condition | Behaviour |
|---|---|
| No SES enclosure | Page explains why; "Not in a bay" table still lists every disk |
| No `server_info.json` | Chassis card reads "Unknown"; labels fall back to slot numbers |
| No `vdev_id.conf` | Labels come from the chassis profile |
| Unknown chassis model | Every real slot shown, single row, numeric labels |
| Helper not installed | LED buttons replaced by a pointer to this README |
| No `smartctl` grant | Bay map fine; SMART panel reports the error |

## What this does not cover

The other two Houston hardware pages — **45Drives System** (CPU/RAM/PCI/NIC/
IPMI inventory) and **45Drives Motherboard** (board diagram) — are out of
scope here. Firmware flashing is deliberately not reimplemented.
