package build

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var (
	progressLine = regexp.MustCompile(`^#(?P<id>\d+)\s+(?P<body>.*)$`)
	stepStart    = regexp.MustCompile(`^\[(?P<label>[^]]*\d+/\d+)\]\s+(?P<display>.+)$`)
	doneLine     = regexp.MustCompile(`^DONE(?:\s+(?P<seconds>[0-9.]+)s)?$`)
	errorLine    = regexp.MustCompile(`^ERROR(?:(?:\s+(?P<seconds>[0-9.]+)s)|(?::.*))?$`)
	transferLine = regexp.MustCompile(`(?i)transferring context:\s+(?P<size>[0-9.]+)(?P<unit>[kMGT]?B)`)
	labelNumbers = regexp.MustCompile(`(\d+)/(\d+)$`)
)

type Step struct {
	Step              string   `json:"step"`
	Opcode            string   `json:"opcode"`
	InstructionSHA256 string   `json:"instruction_sha256"`
	Status            string   `json:"status"`
	DurationSeconds   *float64 `json:"duration_seconds"`
}

type Summary struct {
	ProgressFormat string `json:"progress_format"`
	Total          int    `json:"total"`
	Cached         int    `json:"cached"`
	Rebuilt        int    `json:"rebuilt"`
	Resolved       int    `json:"resolved"`
	Failed         int    `json:"failed"`
	Incomplete     int    `json:"incomplete"`
	Items          []Step `json:"items"`
}

type internalStep struct {
	label, display, status string
	duration               *float64
}

type ProgressParser interface {
	Feed(string) []string
	Summary() Summary
	ContextBytes() int64
	FailureMessages() []string
	EventsSeen() int
	InvalidLines() []string
}

type RawJSONParser struct {
	steps        map[string]*internalStep
	vertexNames  map[string]string
	contextBytes int64
	invalid      []string
	failures     []string
	events       int
}

func NewRawJSONParser() *RawJSONParser {
	return &RawJSONParser{steps: map[string]*internalStep{}, vertexNames: map[string]string{}}
}

func (parser *RawJSONParser) Feed(line string) []string {
	var value struct {
		Vertexes []struct {
			Digest, Name, Started, Completed, Error string
			Cached                                  bool `json:"cached"`
		} `json:"vertexes"`
		Statuses []struct {
			ID, Vertex string
			Current    float64
		} `json:"statuses"`
	}
	if err := json.Unmarshal([]byte(line), &value); err != nil {
		if strings.TrimSpace(line) != "" {
			parser.invalid = append(parser.invalid, truncate(line, 1000))
		}
		return nil
	}
	parser.events++
	var rendered []string
	for _, vertex := range value.Vertexes {
		if vertex.Digest != "" && vertex.Name != "" {
			parser.vertexNames[vertex.Digest] = vertex.Name
		}
		match := stepStart.FindStringSubmatch(vertex.Name)
		if vertex.Digest == "" || match == nil {
			continue
		}
		labels := named(stepStart, match)
		item := parser.steps[vertex.Digest]
		if item == nil {
			item = &internalStep{status: "running"}
			parser.steps[vertex.Digest] = item
		}
		previous := item.status
		item.label = labels["label"]
		item.display = labels["display"]
		if vertex.Error != "" {
			item.status = "failed"
			item.duration = parseDuration(vertex.Started, vertex.Completed)
			parser.failures = append(parser.failures, truncate(vertex.Error, 2000))
		} else if vertex.Completed != "" {
			if vertex.Cached {
				item.status = "cached"
			} else if opcode(item.display) == "FROM" {
				item.status = "resolved"
			} else {
				item.status = "rebuilt"
			}
			item.duration = parseDuration(vertex.Started, vertex.Completed)
		} else if vertex.Started != "" {
			item.status = "running"
		}
		if item.status != "running" && item.status != previous {
			rendered = append(rendered, renderCompletion(item))
		}
	}
	for _, status := range value.Statuses {
		if parser.vertexNames[status.Vertex] == "[internal] load build context" && strings.HasPrefix(status.ID, "transferring context:") && int64(status.Current) > parser.contextBytes {
			parser.contextBytes = int64(status.Current)
		}
	}
	return rendered
}
func (parser *RawJSONParser) Summary() Summary {
	values := make([]*internalStep, 0, len(parser.steps))
	for _, item := range parser.steps {
		values = append(values, item)
	}
	return summarize(values, "rawjson")
}
func (parser *RawJSONParser) ContextBytes() int64 { return parser.contextBytes }
func (parser *RawJSONParser) FailureMessages() []string {
	return append([]string(nil), parser.failures...)
}
func (parser *RawJSONParser) EventsSeen() int        { return parser.events }
func (parser *RawJSONParser) InvalidLines() []string { return append([]string(nil), parser.invalid...) }

type PlainParser struct {
	steps             map[string]*internalStep
	contextBytes      int64
	invalid, failures []string
	events            int
}

func NewPlainParser() *PlainParser { return &PlainParser{steps: map[string]*internalStep{}} }
func (parser *PlainParser) Feed(line string) []string {
	match := progressLine.FindStringSubmatch(strings.TrimSpace(line))
	if match == nil {
		if strings.TrimSpace(line) != "" {
			parser.invalid = append(parser.invalid, truncate(line, 1000))
		}
		return nil
	}
	parser.events++
	values := named(progressLine, match)
	id, body := values["id"], values["body"]
	if transfer := transferLine.FindStringSubmatch(body); transfer != nil {
		fields := named(transferLine, transfer)
		size, _ := strconv.ParseFloat(fields["size"], 64)
		multipliers := map[string]float64{"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}
		parser.contextBytes = int64(size * multipliers[strings.ToLower(fields["unit"])])
	}
	if start := stepStart.FindStringSubmatch(body); start != nil {
		fields := named(stepStart, start)
		parser.steps[id] = &internalStep{label: fields["label"], display: fields["display"], status: "running"}
		return nil
	}
	item := parser.steps[id]
	if item == nil {
		return nil
	}
	if body == "CACHED" {
		zero := 0.0
		item.status = "cached"
		item.duration = &zero
	} else if done := doneLine.FindStringSubmatch(body); done != nil {
		item.status = "rebuilt"
		if opcode(item.display) == "FROM" {
			item.status = "resolved"
		}
		if seconds := named(doneLine, done)["seconds"]; seconds != "" {
			value, _ := strconv.ParseFloat(seconds, 64)
			item.duration = &value
		}
	} else if failed := errorLine.FindStringSubmatch(body); failed != nil {
		item.status = "failed"
		if seconds := named(errorLine, failed)["seconds"]; seconds != "" {
			value, _ := strconv.ParseFloat(seconds, 64)
			item.duration = &value
		}
		parser.failures = append(parser.failures, truncate(body, 2000))
	} else if body == "CANCELED" {
		item.status = "failed"
	} else {
		return nil
	}
	return []string{renderCompletion(item)}
}
func (parser *PlainParser) Summary() Summary {
	values := make([]*internalStep, 0, len(parser.steps))
	keys := make([]int, 0, len(parser.steps))
	byNumber := map[int]*internalStep{}
	for key, value := range parser.steps {
		number, _ := strconv.Atoi(key)
		keys = append(keys, number)
		byNumber[number] = value
	}
	sort.Ints(keys)
	for _, key := range keys {
		values = append(values, byNumber[key])
	}
	return summarize(values, "plain")
}
func (parser *PlainParser) ContextBytes() int64 { return parser.contextBytes }
func (parser *PlainParser) FailureMessages() []string {
	return append([]string(nil), parser.failures...)
}
func (parser *PlainParser) EventsSeen() int        { return parser.events }
func (parser *PlainParser) InvalidLines() []string { return append([]string(nil), parser.invalid...) }

type LayerComparison struct {
	Total            int  `json:"total"`
	New              int  `json:"new"`
	Reused           int  `json:"reused"`
	Removed          int  `json:"removed"`
	MatchingDiffIDs  int  `json:"matching_diff_ids"`
	UnmatchedDiffIDs int  `json:"unmatched_diff_ids"`
	ChangedPositions int  `json:"changed_positions"`
	CommonPrefix     int  `json:"common_prefix"`
	HasBaseline      bool `json:"has_baseline"`
}

func CompareLayers(current, previous []string, hasBaseline bool) LayerComparison {
	available := map[string]int{}
	for _, value := range previous {
		available[value]++
	}
	matching := 0
	for _, value := range current {
		if available[value] > 0 {
			available[value]--
			matching++
		}
	}
	prefix, unchanged := 0, 0
	for index, value := range current {
		if index < len(previous) && value == previous[index] {
			unchanged++
		}
	}
	for prefix < len(current) && prefix < len(previous) && current[prefix] == previous[prefix] {
		prefix++
	}
	maximum := len(current)
	if len(previous) > maximum {
		maximum = len(previous)
	}
	return LayerComparison{len(current), len(current) - matching, matching, len(previous) - matching, matching, len(current) - matching, maximum - unchanged, prefix, hasBaseline}
}

func summarize(values []*internalStep, format string) Summary {
	sort.Slice(values, func(i, j int) bool {
		left := labelNumbers.FindStringSubmatch(values[i].label)
		right := labelNumbers.FindStringSubmatch(values[j].label)
		if left == nil || right == nil {
			return values[i].label < values[j].label
		}
		lt, _ := strconv.Atoi(left[2])
		rt, _ := strconv.Atoi(right[2])
		if lt != rt {
			return lt < rt
		}
		li, _ := strconv.Atoi(left[1])
		ri, _ := strconv.Atoi(right[1])
		return li < ri
	})
	summary := Summary{ProgressFormat: format, Total: len(values)}
	for _, value := range values {
		digest := sha256.Sum256([]byte(value.display))
		item := Step{value.label, opcode(value.display), hex.EncodeToString(digest[:]), value.status, value.duration}
		summary.Items = append(summary.Items, item)
		switch value.status {
		case "cached":
			summary.Cached++
		case "rebuilt":
			summary.Rebuilt++
		case "resolved":
			summary.Resolved++
		case "failed":
			summary.Failed++
		case "running":
			summary.Incomplete++
		}
	}
	return summary
}
func opcode(display string) string {
	fields := strings.Fields(strings.TrimSpace(display))
	if len(fields) == 0 {
		return "UNKNOWN"
	}
	return strings.ToUpper(fields[0])
}
func parseDuration(started, completed string) *float64 {
	if started == "" || completed == "" {
		return nil
	}
	start, err := parseTimestamp(started)
	if err != nil {
		return nil
	}
	end, err := parseTimestamp(completed)
	if err != nil {
		return nil
	}
	value := end.Sub(start).Seconds()
	if value < 0 {
		value = 0
	}
	rounded := float64(int64(value*1000+0.5)) / 1000
	return &rounded
}
func parseTimestamp(value string) (time.Time, error) {
	if index := strings.Index(value, "."); index >= 0 {
		end := strings.IndexAny(value[index:], "Z+-")
		if end > 0 {
			end += index
			fraction := value[index+1 : end]
			if len(fraction) > 9 {
				fraction = fraction[:9]
			}
			fraction += strings.Repeat("0", 9-len(fraction))
			value = value[:index+1] + fraction + value[end:]
		}
	}
	return time.Parse(time.RFC3339Nano, value)
}
func renderCompletion(item *internalStep) string {
	suffix := ""
	if item.duration != nil {
		suffix = fmt.Sprintf(" %.3fs", *item.duration)
	}
	return fmt.Sprintf("%-8s [%s] %s%s", item.status, item.label, item.display, suffix)
}
func named(expression *regexp.Regexp, match []string) map[string]string {
	result := map[string]string{}
	for index, name := range expression.SubexpNames() {
		if index > 0 && name != "" {
			result[name] = match[index]
		}
	}
	return result
}
func truncate(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit]
}
