{{/*
Parse and check the experiment file. Full validation (plug-ins, params) is done by
`robot-run validate` in CI and again by the runner; the chart re-checks what it
needs to render a safe Job.
*/}}
{{- define "robot-sim.experiment" -}}
{{- $raw := required "experiment is required: --set-file experiment=experiments/<name>.yaml" .Values.experiment -}}
{{- $exp := fromYaml $raw -}}
{{- if hasKey $exp "Error" }}{{ fail (printf "experiment is not valid YAML: %s" (get $exp "Error")) }}{{ end -}}
{{- if ne (int (get $exp "schema_version")) 1 }}{{ fail "experiment.schema_version must be 1" }}{{ end -}}
{{- $name := toString (get $exp "name") -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$" $name) }}{{ fail "experiment.name must be a DNS label of at most 40 characters" }}{{ end -}}
{{- $shards := int (default 1 (get $exp "shards")) -}}
{{- $parallelism := int (default 1 (get $exp "parallelism")) -}}
{{- $limits := default dict (get $exp "limits") -}}
{{- if not (hasKey $limits "max_gpus") }}{{ fail "experiment.limits.max_gpus is required" }}{{ end -}}
{{- $gpus := int (ternary 1 (get $limits "gpus_per_shard") (not (hasKey $limits "gpus_per_shard"))) -}}
{{- $deadline := int (default 0 (get $limits "active_deadline_seconds")) -}}
{{- if or (lt $shards 1) (gt $shards 1000) }}{{ fail "experiment.shards must be between 1 and 1000" }}{{ end -}}
{{- if or (lt $parallelism 1) (gt $parallelism $shards) }}{{ fail "experiment.parallelism must be between 1 and shards" }}{{ end -}}
{{- if lt $gpus 0 }}{{ fail "experiment.limits.gpus_per_shard must be >= 0" }}{{ end -}}
{{- if gt (mul $parallelism $gpus) (int (get $limits "max_gpus")) }}{{ fail "parallelism x gpus_per_shard exceeds experiment.limits.max_gpus" }}{{ end -}}
{{- if and (get $exp "sync") (ne $parallelism $shards) }}{{ fail "experiment.sync requires parallelism == shards (and enough actual GPU nodes)" }}{{ end -}}
{{- $adapter := toString (get (default dict (get $exp "adapter")) "name") -}}
{{- dict "name" $name "adapter" $adapter "shards" $shards "parallelism" $parallelism "gpus" $gpus "deadline" $deadline "sha" (sha256sum $raw) | toJson -}}
{{- end -}}
