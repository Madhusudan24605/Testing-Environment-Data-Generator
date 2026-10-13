# Testing-Environment-Data-Generator

Testing environment data generator for the LAB University EV Powertrain Digital Twin project. A Python tool generating realistic simulated EV data — battery, charging, powertrain, grid, and trip data — so other teams can build and test without needing real hardware.

## Group members
1. Yeganeh Maleki
2. Madhusudan Regmi

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


## ✅ Issues We Found and Fixed

### 1. Record generation was capped at 1,000 for no real reason

**What we found**
The tool had a hardcoded limit of 1,000 records per run, with no clear reasoning behind why 1,000 specifically was chosen.

**Why this mattered**
Our tool's whole purpose is to supply test data for other groups — visualization, predictive modeling, and the wider powertrain/analytics group. If any of them needed more data, our cap would have blocked them with no way around it except editing the code themselves.

**What we did**
Raised the limit to 10,000. This is a single value that controls both the UI's input limit and the generation loop's upper bound.

**Verified**
Tested by generating 10,000 records multiple times — worked every time, zero errors, under a second.

---

### 2. Record IDs could accidentally duplicate

**What we found**
Every charging session, trip, and Kempower session gets an ID (like CHG-1234). These used to be random numbers, which meant two different records could end up with the exact same ID, especially once we started generating more records.

**Why this mattered**
Duplicate IDs are a real data integrity problem for anyone referencing records by ID.

**What we did**
IDs now count up in order (CHG-0001, CHG-0002, ...) instead of being random, guaranteeing every one is unique.

**Verified**
Generated 10,000 records of each type and checked for duplicates — found none.

---

### 3. Trip distance, speed, and duration didn't match each other

**What we found**
These three values used to each be picked completely independently. That meant the data could describe something physically impossible, like a 600 km trip that supposedly took 5 minutes.

**Why this mattered**
Data that's physically impossible is misleading for any group trying to build or test a model on top of it.

**What we did**
Duration is now calculated as distance ÷ speed, so the numbers always agree.

**Verified**
Checked on 5,000 generated trips — matched in every single case.

---

### 4. Named charging stations appeared in random locations

**What we found**
Stations like "Lahti Supercharger" used to get a fully random GPS point anywhere in Finland. 459 of 496 "Lahti Supercharger" rows were more than 50 km from the real city.

**Why this mattered**
A station named after a real place showing up hundreds of kilometers away makes the data look fake and breaks map visualizations.

**What we did**
Each named location now has a real anchor coordinate, with the generated point placed about ±1 km around it.

**Verified**
0 of 2,540 Lahti rows tested were farther than 3 km from the real city, after the fix.

---

### 5. The map slowed down with large amounts of data

**What we found**
Drawing every single point on the map (all 10,000 at once) made zooming and panning slow and laggy.

**Why this mattered**
Once we raised the record limit, the map needed to stay usable at that size.

**What we did**
The map now draws a smart sample of points for display. The table, statistics panel, and every exported file still contain every single record — nothing is lost, just drawn more efficiently.

---

### 6. Documentation was missing

**What we did**
Added a proper README explaining what the tool does, how to run it, its data types, current settings, and known limitations — written for anyone opening the repository for the first time.

---

## 🔄 Issues We're Currently Fixing

### 7. Powertrain Data — Speed, Motor RPM, and Torque Are Not Physically Connected

**What we found**
While reviewing generated Powertrain & Motor records, we noticed the values didn't make physical sense together. The tool picks `speed_kmh`, `motor_rpm`, and `torque_nm` completely separately — each one is just an independent random number. In a real electric vehicle, these are connected: if a car is going fast, the motor is spinning fast too, and torque changes depending on how fast the motor is spinning. We found records showing a car moving very slowly while the motor was spinning at almost maximum RPM — a combination that could never actually happen in a real car.

**Why this matters**
This data is meant to help other groups build and test real models. If the underlying physics is wrong, anything built on top of it inherits that wrongness.

**What we're doing**
A separate version of the code now calculates motor RPM from speed using the car's actual wheel size and gear ratio, and calculates torque from a realistic motor torque curve instead of random numbers.

**Current status**
This work is real and already built, but it lives in a separate, unmerged version of the code. It still needs to be combined into one final file and re-tested.

---

### 8. Haven't tested the full app at large scale yet

**What we found**
The logic behind the fixes above has been tested and verified in the code directly. What hasn't been tested is actually running the full app with 10,000 records loaded at once.

**Why this matters**
Something could still break only at full scale — slow table rendering, map lag, or export issues — that smaller tests wouldn't catch.

**What we're doing**
Still need to check the table, the map, and all four export formats (CSV, JSON, SQL, SQLite) work correctly with the full 10,000 records loaded.

---

## 📋 What's Next

### 9. Charging energy and battery limit mismatches

**What we found**
About 40% of generated charging records show an energy amount that doesn't line up with the battery's actual charge increase. Separately, about 15% of generated trips (745 of 5,000 tested) require more energy than the largest available battery actually has.

**Why this matters**
Both are the same category of problem as the powertrain physics issue — numbers that don't physically agree with each other.

**What's needed**
Calculate charging energy from the battery's actual charge change, and cap trip energy so it never exceeds what's physically possible.

---

### 10. Random GPS points can land outside Finland

**What we found**
Random location points are currently chosen from a rectangle drawn around Finland, which includes areas that aren't actually Finland — sea, parts of Sweden, Russia, Norway.

**What's needed**
Check generated points against Finland's real shape, not just a rectangular box.

---

### 11. No automated tests exist

**What we found**
All verification so far has been done manually — generating data and checking it by hand.

**Why this matters**
Manual checking relies on someone remembering to do it, and won't automatically catch a regression next time the code changes.

**What's needed**
Build automated tests that check these things on their own.

---

### 12. Considering a real map view instead of the current outline

**What we found**
The current GPS map is a simplified, hand-drawn outline of Finland — functional, but not visually polished.

**What we're considering**
Switching to a real map (like Google Maps) for a more professional look during demos. Decided to hold off for now — it would require internet and an API key, breaking the app's current offline capability, plus real engineering work to embed in our framework. Nothing has been built yet, this is still just research.
