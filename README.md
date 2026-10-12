# Testing-Environment-Data-Generator
Testing environment data generator for the LAB University EV Powertrain Digital Twin project. A Python tool generating realistic simulated EV data — battery, charging, powertrain, grid, and trip data — so other teams can build and test without needing real hardware.

Group members
1. Yeganeh maleki
2. Madhusudan Regmi
3. Md. Nihon Mostari Siam


-------------------------------------------------------------------------------------------------------------------------------------------------

l. Record Generation Limit — Capped at 1,000, Not Based on Actual Need


- What we found

While reviewing the tool’s settings, we found that record generation was hardcoded to a maximum of 1,000 records per run. This limit existed as a single fixed number in the app’s configuration, with no clear reasoning behind why 1,000 specifically was chosen — it wasn’t based on what other groups in the project actually need.

- Why this mattered

Our tool’s whole purpose is to supply test data for other groups — visualization, predictive modeling, and the wider powertrain/analytics group. If any of them needed a larger dataset (for example, to properly train or test a predictive model, which often benefits from more data), our cap would have blocked them before they even started, with no way around it except changing the code themselves.

- What we did

We raised the limit in the app’s configuration:

python
max_records: int = 10_000  # raised from 1,000 to support larger datasets

This was a single-line change — the value controls both the UI’s input limit and the generation loop’s upper bound throughout the whole app, so nothing else needed to change alongside it.

We tested it by actually generating 10,000 records multiple times. It worked every time, with no errors, in under a second.

- Advantage

Other groups are no longer blocked by an arbitrary limit that was never actually justified by real project needs. Raising this cap removed a real constraint on the rest of the project’s ability to build and test using our data.

- Still open

We haven’t yet confirmed with the dependent groups (visualization, predictive modeling, powertrain/analytics) what dataset sizes they actually expect to need. 10,000 was a reasonable jump from 1,000, but it was our own estimate, not a number agreed with them — worth checking before we call this fully settled.
