# OreSat context

Pointers for reviewing an OreSat card (github.com/oresat), collected from earlier
reviews in November 2026. Verify against the repositories; they move.

- Default branches are `master`.
- Libraries: `oresat-kicad` (`ORESAT_LIBRARIES`, cloned for you in `/work/libs`).
- Cards plug into `oresat-backplane`. The C3 card (`oresat-c3-hardware`) switches power
  to each card through its OreSat power domain (OPD) block; its watchdog drives the
  shared `~SD` line, open collector.
- Software that defines the hardware interface: `oresat-c3-software`
  (`oresat_c3/subsystems/opd.py`: MAX7310 pin roles per processor type) and
  `oresat-configs` (`cards.csv`: OPD addresses, which cards are always on).
- Cards with the OSD3358 (Octavo) processor share the OPD block, the OSD3358 sheets
  and the debug connector pinout: `oresat-dxwifi-hardware`, `oresat-star-tracker-hardware`,
  `oresat-gps-hardware`, `oresat-cfc-hardware`. Compare a block with a sibling card to
  find drift.
- MCXN947 cards follow `oresat-proto-card` (`mcxn-proto-card/`): MCU, OPD, CAN and debug
  connector block.
- Battery card: hardware `oresat-batteries`, firmware `oresat-battery-app` (MCXN board
  files on branch `feature-mcxn-board`); the approved inhibit diagram is
  `inhibits-block-digram/` in the hardware repository.
- Other system context: `oresat-solar-hardware`, `oresat-end-cards`.
