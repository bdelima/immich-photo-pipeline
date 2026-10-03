# immich-photo-pipeline

Automated pipeline that processes Immich staging-album photos (Collage Maker, Wallpaper Maker) through a matting/cropping recipe, a Review approval step, and syncs approved managed albums to a Live album, with a small web UI to pick the live album.

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow does the rest. Changes reach `main` only
through pull requests.
