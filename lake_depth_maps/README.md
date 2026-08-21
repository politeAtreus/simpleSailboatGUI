# Static lake depth grids

Each supported lake keeps its numeric bathymetry in `depth_grid.npz`.

The GUI does not inspect or interpret the visual contour overlay.  A grid contains:

- `depth_m`: 2-D `float32` array; `NaN` means land/unmapped.
- `north`, `south`, `west`, `east`: geographic bounds of the north-up grid.
- `spacing_m`: nominal cell spacing.
- `uncertainty_m`: nominal +/- uncertainty displayed by the GUI.
- `source`: source PDF/page used to prepare the grid.

Current custom grids:

- `Lake_Micmac/depth_grid.npz` - 1 m spacing, nominal +/- 1.0 m.
- `Lake_Charles/depth_grid.npz` - 1 m spacing, nominal +/- 1.0 m.

The 1 m value is storage/sampling resolution, not bathymetric accuracy.  These grids are approximate historical-map reconstructions and are not a substitute for current hydrographic survey or onboard depth sensing.
