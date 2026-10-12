# Testing-Environment-Data-Generator

Testing environment data generator for the LAB University EV Powertrain Digital Twin project. A Python tool generating realistic simulated EV data — battery, charging, powertrain, grid, and trip data — so other teams can build and test without needing real hardware.

## Group members
1. Yeganeh Maleki
2. Madhusudan Regmi
3. Md. Nihon Mostari Siam

## What it does
This app generates realistic fake EV data for six categories, so other groups in the project can build dashboards, models, and analytics before real hardware or real vehicle data is available:
- Battery Sensor data
- Charging Session data
- Powertrain & Motor data
- Grid & Energy data
- Vehicle Trip data
- Kempower Station Session data

Data can be exported as CSV, JSON, SQL, or SQLite.

## Requirements
- Python 3.x
- No extra installs needed beyond Python itself (the app uses Python's built-in libraries, including Tkinter for the interface)


## Getting started

python emdt_test_data_generator.py


## Current settings
- Up to 10,000 records can be generated per run (raised from the original 1,000 limit)
- Live statistics panel (min / mean / max) for generated data
- Map view showing generated GPS points within Finland
- Manual record entry, with auto-generated unique IDs

## Known limitations
- Charging energy doesn't always match the battery's actual charge change (around 40% of charging records are off by more than 20 kWh from what the battery's charge level implies)
- Some trips (about 15%) require more energy than the largest available battery can actually hold
- Random GPS points are chosen from a rectangle around Finland, so a small number can land outside Finland's real borders (sea, neighboring countries)
- Using the same "seed" doesn't produce fully identical results on repeat runs — timestamps still use the current real time
- Export and Delete buttons stay clickable while data is still generating in the background
- Charging station and vehicle specification details are approximate and haven't been verified against official sources yet
- No automated tests exist yet — all checking has been done manually

## Roadmap / Future ideas
- Merge in the physics-based upgrade (speed, motor RPM, torque, and battery values calculated from real vehicle physics instead of random numbers) — this already exists in a separate version of the code and still needs to be combined and tested
- Test the full app with a large dataset (10,000 records) to confirm the table, map, and all export formats handle it smoothly
- Fix the known limitations listed above
- Considering switching the map from a simplified outline to real map tiles (see "Considered" note in the backlog) — not started yet
