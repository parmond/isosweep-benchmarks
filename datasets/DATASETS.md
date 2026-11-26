# Datasets

Seven real datasets are used by the benchmark suite. They are not stored in
this repository: `fetch_datasets.py` rebuilds each one from its public source
and checks it against `MANIFEST.sha256`, the checksums used by the benchmark
specifications and the published results. The synthetic industry fields are
generated on the fly by `tools/fieldgen` (deterministic, from a seed).

| File | Source | Preparation | Benchmarks |
|---|---|---|---|
| `cop_N46E007.tif` | [Copernicus GLO-30 DEM](https://copernicus-dem-30m.s3.amazonaws.com/readme.html), tile `Copernicus_DSM_COG_10_N46_00_E007_00_DEM` | none (3600 × 3600, 398–4472 m) | `dem-alps-tile`, `dem-tile-512` (window 512 × 512 at x 3000, y 1400), `dem-tile-512-bands` |
| `alps_7200.tif` | Copernicus GLO-30, tiles N45/N46 × E007/E008 | `gdalbuildvrt` of the 4 tiles, then `gdal_translate -co TILED=YES -co COMPRESS=DEFLATE` (7200 × 7200) | `dem-alps-mosaic` |
| `gmrt_mar_filled.tif` | [GMRT GridServer](https://www.gmrt.org/services/GridServer), Mid-Atlantic Ridge, 46–42° W, 26–30° N, high resolution | 0.15% voids filled with the nearest valid cell, NoData flag removed (1823 × 2063) | `gmrt-bathy` |
| `gfs_prmsl.f32` | NOAA GFS 0.25°, 2026-11-18 00z analysis, `PRMSL:mean sea level` ([NOAA Open Data Dissemination](https://registry.opendata.aws/noaa-gfs-bdp-pds/)) | decoded with GDAL to raw float32, Pa (1440 × 721) | `gfs-mslp`, `gfs-mslp-bands` |
| `gfs_t2m.f32` | same file, `TMP:2 m above ground` | raw float32, °C (1440 × 721) | `gfs-t2m` |
| `hrrr_mslp.f32` | NOAA HRRR 3 km, 2026-11-18 00z, `MSLMA:mean sea level` ([NOAA Open Data Dissemination](https://registry.opendata.aws/noaa-hrrr-pds/)) | raw float32, Pa (1799 × 1059) | `hrrr-mslp`, `hrrr-mslp-bands` |
| `hrrr_t2m.f32` | same file, `TMP:2 m above ground` | raw float32, °C (1799 × 1059) | `hrrr-t2m` |

## Licences and attribution

- Copernicus GLO-30: © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH
  2014–2018, provided under COPERNICUS by the European Union and ESA.
- GMRT: Ryan, W.B.F. et al. (2009), Global Multi-Resolution Topography
  synthesis, *Geochem. Geophys. Geosyst.* 10, Q03014. CC BY 4.0.
- NOAA GFS and HRRR: public domain.

## Reproducibility

Tested in November 2026 with GDAL 3.13.3: six of the seven files rebuild byte
for byte from their public sources, including the Alps mosaic. The GMRT grid
rebuilds with values identical to the published benchmark field (its field
fingerprint in the results matches), but the GMRT service now encodes the
GeoTIFF differently, so its file checksum differs and the harness refuses it
for `gmrt-bathy`. `fetch_datasets.py` reports this case as "VALUES OK, BYTES
DIFFER". A future revision of the benchmark specifications will pin the
decoded values instead of the file bytes for this dataset.
