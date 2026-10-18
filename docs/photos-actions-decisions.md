# Photos view: decisions behind search, selection and Send to

Settled with Bob from the "Household Photos Wireframe". The README describes what the view does; this records why, and what is deliberately not there.

- **Already in the pipeline is hidden by default.** A photo whose original is a source of any photo in the library is left out of the timeline and search. "Show photos already in the pipeline" (search filters, Library group) shows them again, tagged "In pipeline" (green).
- **One processed photo per original, for now.** A send skips any original already in the pipeline. A second processed version of the same original is not supported: intake skips every asset id it already knows (`known_source_ids`), so allowing it needs an intake change.
- **The mark is derived, never stored.** It is worked out from the library each time (`household.pipeline_states`), so it clears when the processed photo is deleted for good, and a restore from the web trash brings it back.
- **In the pipeline's trash.** Such a photo is tagged "In trash" (amber). Choosing a Send to destination opens a dialog offering to send and purge. It is one request (`/api/household/send` with `purge: true`): the original is added to the entry queue first, and only if that worked (or it was already waiting there) are the processed photos made from it deleted for good (a collage goes as a whole), which makes the original eligible again. A send that is refused or skipped never purges, so a failed send cannot cost anyone a processed photo. The original is never touched.
- **A leftover original in an entry album.** If an original could not be cleared from its entry album (Immich lets only the account that added it do so) and its processed photo is then purged, the next cycle would process it again. Not handled.
- **Trash on any household photo** goes to the Immich trash of the account that owns it, after a confirmation. The pipeline's own copies are not touched. Sending and trashing use the owner's key (`IMMICH_EXTRA_API_KEY`), because Immich only lets an owner add their photo to an album or trash it.
- **Naming.** The third destination is "Import Only" (the app's name for it; the wireframe said "Importer").
- **People filter** is empty until Immich has named people (the face rebuild).
- **Not built yet:** a Collage Maker place (select 2 to 12 mixed photos and make a collage; it changes intake, the worker and the recipe), and editing tools.
- **Unverified against a real Immich:** the search request fields (`ocr`, `isNotInAlbum`, `visibility`, `personIds`, `lensModel`), `/search/suggestions`, `/people` and `/download/archive`. Checked only against a fake.
