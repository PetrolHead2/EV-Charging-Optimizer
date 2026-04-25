# EV Charging Optimizer for Home Assistant

Cost-optimized EV charging using Nordpool 15-minute prices,
pyscript, and native Home Assistant automations.

## Features
- Selects cheapest Nordpool slots before your departure deadline
- Weekly departure schedule grid — no daily interaction needed
- Power tariff protection (Swedish effekttariff)
- Real-time consumption guard using Tibber Pulse
- Hysteresis to prevent rapid charger toggling
- Mercedes Me SoC integration for safety stop
- Zaptec charger integration

## Hardware
- Charger: Zaptec
- Vehicle: Mercedes-Benz PHEV (Mercedes Me integration)
- Energy monitor: Tibber Pulse
- Electricity market: Nordpool SE3

## Installation
See CLAUDE.md for full entity reference, architecture details, and setup instructions.

### Prerequisites
- Home Assistant (Docker)
- HACS with pyscript integration installed
- Nordpool integration (15-minute prices)
- Zaptec integration
- Mercedes Me integration
- Tibber integration

### File locations
| File | HA config path |
|------|---------------|
| `pyscript/ev_optimizer.py` | `/config/pyscript/` |
| `pyscript/ev_control_loop.py` | `/config/pyscript/` |
| `packages/ev_optimizer.yaml` | `/config/packages/` |
| `www/ev_schedule_grid.html` | `/config/www/` |

## Configuration
1. Copy files to their HA config locations above
2. Add your long-lived access token to `www/ev_schedule_grid.html`
3. Update entity IDs in CLAUDE.md to match your setup
4. Install pyscript and add to `configuration.yaml`:
   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```
5. Restart HA and trigger first recompute via Developer Tools → Services → `pyscript.ev_optimizer_recompute`

## License
MIT
