You receive the drafted sources of a semantic catalog: each with its
fields, semantic types, and the entity and activity names it claims.

Define the shared vocabulary and the joins:
- entities: define every entity name used by any source, with a
  description and the keywords/synonyms people use for it in questions
  (plural forms included). Mark row_level true only for the entity that
  means "the records themselves" (e.g. event).
- activities: define every activity name used by any source, with a
  description, the verbs/nouns people use for it, the semantic_type of
  the thing the activity is about (resource), and the roles of the
  resource and of the actor fields.
- relationships: pairs of fields in *different* sources that hold the
  same value and can be joined (same_entity / references /
  parent_child), each with a confidence in [0, 1] reflecting how sure
  you are they truly match. Only propose joins you'd trust.
