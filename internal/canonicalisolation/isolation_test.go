//go:build canonicalisolation

// SPDX-License-Identifier: GPL-3.0-or-later
package canonicalisolation

import (
	"bytes"
	"context"
	"debug/buildinfo"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/canonicalisolation/protocol"
)

// This package intentionally links only stdlib and the stdlib-only protocol.
// The worker is a different command/package/binary, not a TestMain branch that
// accidentally executes BSC/Prysm import initialization in the supervisor.
var workerPath string
var buildOnce sync.Once
var buildErr error
var slots = make(chan struct{}, 2)
var activity struct {
	sync.Mutex
	active, peak int
}

func TestMain(m *testing.M) {
	if len(os.Args) == 2 {
		switch os.Args[1] {
		case "--hang-child":
			time.Sleep(time.Minute)
			os.Exit(0)
		case "--failed-child":
			os.Exit(7)
		case "--output-flood":
			fmt.Print(strings.Repeat("x", protocol.MaxOutput+1))
			os.Exit(0)
		case "--fake-success":
			fmt.Print(`{"accepted":true,"stage":"accepted","fixture_id":"wrong","request_digest":"wrong","candidate_digest":"wrong"}`)
			os.Exit(0)
		}
	}
	dir, err := os.MkdirTemp("", "canonical-one-shot-")
	if err != nil {
		panic(err)
	}
	workerPath = filepath.Join(dir, "worker")
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

func worker(t *testing.T) string {
	t.Helper()
	buildOnce.Do(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "go", "build", "-race", "-tags=canonicalisolation", "-o", workerPath, "./worker")
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		start := time.Now()
		t.Logf("worker build start: argv=%q", cmd.Args)
		buildErr = cmd.Run()
		t.Logf("worker build finished: elapsed=%s err=%v", time.Since(start), buildErr)
	})
	if buildErr != nil {
		t.Fatalf("worker build: %v", buildErr)
	}
	return workerPath
}

type boundedBuffer struct {
	bytes.Buffer
	overflow bool
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	space := protocol.MaxOutput - b.Len()
	if len(p) > space {
		b.overflow = true
		p = p[:space]
	}
	_, _ = b.Buffer.Write(p)
	return n, nil
}

type processResult struct {
	PID      int
	Waited   bool
	ExitCode int
	Elapsed  time.Duration
}

func execute(ctx context.Context, path string, args []string, raw []byte) ([]byte, processResult, error) {
	// The 60s deadline includes waiting for the per-supervisor slot.
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, processResult{}, err
	}
	select {
	case slots <- struct{}{}:
	case <-ctx.Done():
		return nil, processResult{}, ctx.Err()
	}
	defer func() { <-slots }()
	if err := ctx.Err(); err != nil {
		return nil, processResult{}, err
	}
	cmd := exec.CommandContext(ctx, path, args...)
	cmd.WaitDelay = 2 * time.Second
	cmd.Stdin = bytes.NewReader(raw)
	for _, s := range os.Environ() {
		if !strings.HasPrefix(s, "GOMAXPROCS=") {
			cmd.Env = append(cmd.Env, s)
		}
	}
	cmd.Env = append(cmd.Env, "GOMAXPROCS=2")
	var out, errout boundedBuffer
	cmd.Stdout = &out
	cmd.Stderr = &errout
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return nil, processResult{}, err
	}
	stats := processResult{PID: cmd.Process.Pid}
	activity.Lock()
	activity.active++
	if activity.active > activity.peak {
		activity.peak = activity.active
	}
	activity.Unlock()
	// Wait MUST finish before decrementing active/releasing the slot/accepting output.
	err := cmd.Wait()
	stats.Elapsed = time.Since(start)
	stats.Waited = cmd.ProcessState != nil
	if stats.Waited {
		stats.ExitCode = cmd.ProcessState.ExitCode()
	}
	activity.Lock()
	activity.active--
	activity.Unlock()
	if out.overflow || errout.overflow || errout.Len() != 0 {
		return nil, stats, errors.New("unexpected or oversized child output")
	}
	if ctx.Err() != nil {
		return nil, stats, ctx.Err()
	}
	return bytes.Clone(out.Bytes()), stats, err
}

func call(ctx context.Context, path string, args []string, raw []byte) (protocol.Response, processResult, error) {
	output, stats, runErr := execute(ctx, path, args, raw)
	var result protocol.Response
	if err := protocol.StrictJSON(output, &result); err != nil {
		return result, stats, fmt.Errorf("invalid child result: %w (process: %v)", err, runErr)
	}
	if !stats.Waited || result.FixtureID != protocol.FixtureID || result.RequestDigest != protocol.Digest(raw) {
		return protocol.Response{}, stats, errors.New("unbound child result")
	}
	var request protocol.Request
	if protocol.StrictJSON(raw, &request) == nil && len(raw) <= protocol.MaxInput && result.CandidateDigest != protocol.Digest(request.Candidate) {
		return protocol.Response{}, stats, errors.New("candidate digest mismatch")
	}
	if result.CandidateVisibleBeforeVerify {
		return protocol.Response{}, stats, errors.New("candidate visible before verification")
	}
	if runErr != nil {
		if stats.ExitCode != 2 || result.Accepted || result.Error == "" || result.CandidateHash != "" || result.SnapshotDigest != "" || result.Justified != 0 || result.Finalized != 0 {
			return protocol.Response{}, stats, errors.New("inconsistent child rejection")
		}
		return result, stats, fmt.Errorf("child rejected at %s: %s", result.Stage, result.Error)
	}
	if !result.Accepted || result.Stage != "accepted" || result.Error != "" || result.CandidateHash == "" || result.SnapshotDigest == "" || result.Justified != 3072 || result.Finalized != 3071 {
		return protocol.Response{}, stats, errors.New("inconsistent child success")
	}
	expected := 1
	if request.Replay {
		expected = 3073
	}
	if result.VerifiedHeaders != expected {
		return protocol.Response{}, stats, errors.New("incorrect full-verification count")
	}
	return result, stats, nil
}
func run(t *testing.T, request protocol.Request) (protocol.Response, processResult, error) {
	t.Helper()
	raw, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) > protocol.MaxInput {
		t.Fatal("test request exceeds envelope bound")
	}
	r, s, err := call(context.Background(), worker(t), nil, raw)
	t.Logf("worker process PID=%d waited=%v exit=%d elapsed=%s (Start through Wait; excludes slot wait)", s.PID, s.Waited, s.ExitCode, s.Elapsed)
	return r, s, err
}

func loadBundle(t *testing.T) protocol.Bundle {
	t.Helper()
	dir := os.Getenv("BSC_CANONICAL_FIXTURE_DIR")
	if dir == "" {
		t.Fatal("set BSC_CANONICAL_FIXTURE_DIR to independently reproduced modern-wire artifacts")
	}
	read := func(name string) []byte {
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	b := protocol.Bundle{Config: read("config.json"), Headers: read("headers.rlp"), Support: read("snapshot-2048.json"), Anchor: read("snapshot-3072.json")}
	if err := b.Authenticate(); err != nil {
		t.Fatal(err)
	}
	return b
}

var fixtureOnce sync.Once
var fixtureRequest protocol.Request
var fixtureCandidates protocol.Candidates
var fixtureErr error

func fixture(t *testing.T) (protocol.Request, protocol.Candidates) {
	t.Helper()
	fixtureOnce.Do(func() {
		start := time.Now()
		defer func() { t.Logf("fixture preparation elapsed=%s", time.Since(start)) }()
		phase := time.Now()
		b := loadBundle(t)
		t.Logf("fixture load/authenticate elapsed=%s", time.Since(phase))
		phase = time.Now()
		raw, err := json.Marshal(b)
		t.Logf("fixture encode elapsed=%s", time.Since(phase))
		if err != nil {
			fixtureErr = err
			return
		}
		output, stats, err := execute(context.Background(), worker(t), []string{"--candidates"}, raw)
		t.Logf("fixture generator PID=%d waited=%v exit=%d elapsed=%s (Start through Wait; excludes slot wait)", stats.PID, stats.Waited, stats.ExitCode, stats.Elapsed)
		if err != nil || !stats.Waited || stats.ExitCode != 0 {
			fixtureErr = fmt.Errorf("fixture generator: %+v %v", stats, err)
			return
		}
		if err := protocol.StrictJSON(output, &fixtureCandidates); err != nil {
			fixtureErr = err
			return
		}
		if !fixtureCandidates.Controls || len(fixtureCandidates.Good) == 0 || len(fixtureCandidates.WrongMessage) == 0 || len(fixtureCandidates.Good) > protocol.MaxRecord || len(fixtureCandidates.WrongMessage) > protocol.MaxRecord {
			fixtureErr = errors.New("generator controls missing")
			return
		}
		fixtureRequest = protocol.Request{FixtureID: protocol.FixtureID, Bundle: b, Candidate: fixtureCandidates.Good, Snapshots: []uint64{2048, 3072}}
		for n := uint64(2000); n < protocol.CandidateNumber; n++ {
			fixtureRequest.Retained = append(fixtureRequest.Retained, n)
		}
		t.Logf("fresh generator PID=%d, original/wrong-message pairing and re-signed proposer controls passed; candidate RLP SHA256 good=%s wrong=%s", stats.PID, protocol.Digest(fixtureCandidates.Good), protocol.Digest(fixtureCandidates.WrongMessage))
	})
	if fixtureErr != nil {
		t.Fatal(fixtureErr)
	}
	// Deep copy request slices so concurrent mutation cases never share state.
	raw, err := json.Marshal(fixtureRequest)
	if err != nil {
		t.Fatal(err)
	}
	var req protocol.Request
	if err := json.Unmarshal(raw, &req); err != nil {
		t.Fatal(err)
	}
	return req, fixtureCandidates
}
func checkGood(t *testing.T, r protocol.Response, s processResult, err error) {
	t.Helper()
	if err != nil || !r.Accepted || !s.Waited || s.ExitCode != 0 || r.CandidateHash != protocol.CandidateHash || r.SnapshotDigest != protocol.ExpectedSnapshot {
		t.Fatalf("invalid success: %+v %+v %v", r, s, err)
	}
}

func TestCanonicalOfficialCases(t *testing.T) {
	_, candidates := fixture(t)
	cases := []struct {
		name        string
		modify      func(*protocol.Request)
		stage, want string
	}{
		{"valid", func(*protocol.Request) {}, "accepted", ""},
		{"wrong_message_correct_proposer", func(r *protocol.Request) { r.Candidate = candidates.WrongMessage }, "verify", "invalid attestation, signature verify failed"},
		{"missing_epoch", func(r *protocol.Request) { r.Retained = r.Retained[1:] }, "verify", "unknown ancestor"},
		{"missing_parent", func(r *protocol.Request) { r.Retained = r.Retained[:len(r.Retained)-1] }, "verify", "unknown ancestor"},
		{"missing_support_snapshot", func(r *protocol.Request) { r.Snapshots = r.Snapshots[1:] }, "verify", "unknown ancestor"},
		{"missing_all_snapshots", func(r *protocol.Request) { r.Snapshots = nil }, "verify", "unknown ancestor"},
		{"wrong_config", func(r *protocol.Request) { r.Bundle.Config = append(r.Bundle.Config, ' ') }, "admission", "context does not match compiled synthetic provenance"},
		{"wrong_snapshot", func(r *protocol.Request) { r.Bundle.Support = append(r.Bundle.Support, ' ') }, "admission", "context does not match compiled synthetic provenance"},
		{"wrong_context", func(r *protocol.Request) { r.Bundle.Headers[100] ^= 1 }, "admission", "context does not match compiled synthetic provenance"},
		{"duplicate_selection", func(r *protocol.Request) { r.Retained[1] = r.Retained[0] }, "admission", "retained headers must be unique ascending subset of [2000,3072]"},
		{"too_many_headers", func(r *protocol.Request) { r.Retained = append(r.Retained, 3072) }, "admission", "request outside fixture bounds"},
		{"too_many_snapshots", func(r *protocol.Request) { r.Snapshots = append(r.Snapshots, 3072) }, "admission", "request outside fixture bounds"},
		{"candidate_trailing", func(r *protocol.Request) { r.Candidate = append(bytes.Clone(r.Candidate), 0x80) }, "admission", "rlp: input contains more than one value"},
		{"candidate_oversize", func(r *protocol.Request) { r.Candidate = make([]byte, protocol.MaxRecord+1) }, "admission", "request outside fixture bounds"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, _ := fixture(t)
			tc.modify(&req)
			r, s, err := run(t, req)
			if tc.stage == "accepted" {
				checkGood(t, r, s, err)
				return
			}
			if err == nil || r.Accepted || r.Stage != tc.stage || (tc.want != "" && r.Error != tc.want) || !s.Waited || s.ExitCode != 2 {
				t.Fatalf("wrong rejection: %+v %+v %v", r, s, err)
			}
			t.Logf("exact error=%q, reads=%d misses=%d PID=%d waited=%v", r.Error, r.Reads, r.Misses, s.PID, s.Waited)
		})
	}
}

func TestCanonicalFullReplay(t *testing.T) {
	req, _ := fixture(t)
	req.Replay = true
	r, s, err := run(t, req)
	checkGood(t, r, s, err)
	if r.VerifiedHeaders != 3073 {
		t.Fatal("full suffix was not cryptographically verified")
	}
	t.Logf("fresh PID=%d, VerifyHeader calls=%d; no seed snapshots imported; expected hash/snapshot digest match", s.PID, r.VerifiedHeaders)
}

func TestCanonicalConcurrentProcesses(t *testing.T) {
	good, candidates := fixture(t)
	type answer struct {
		index int
		r     protocol.Response
		s     processResult
		err   error
	}
	results := make(chan answer, 8)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			req := good
			if i%2 == 1 {
				req.Candidate = candidates.WrongMessage
			}
			r, s, err := run(t, req)
			results <- answer{i, r, s, err}
		}(i)
	}
	close(start)
	wg.Wait()
	close(results)
	seen := map[int]bool{}
	for got := range results {
		if got.index%2 == 0 {
			checkGood(t, got.r, got.s, got.err)
		} else if got.err == nil || got.r.Stage != "verify" || got.r.Error != "invalid attestation, signature verify failed" || !got.s.Waited || got.s.ExitCode != 2 {
			t.Fatalf("bad rejection: %+v", got)
		}
		if got.s.PID <= 0 || seen[got.s.PID] {
			t.Fatalf("shared child PID: %d", got.s.PID)
		}
		seen[got.s.PID] = true
	}
	activity.Lock()
	peak, active := activity.peak, activity.active
	activity.Unlock()
	if peak != cap(slots) || cap(slots) != 2 || active != 0 || len(slots) != 0 {
		t.Fatalf("child lifecycle bound broken: peak=%d active=%d slots=%d", peak, active, len(slots))
	}
	t.Logf("eight requests, eight fresh PIDs, observed peak=%d, zero active children after Wait", peak)
}

func TestSupervisorFailureBounds(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Millisecond)
	_, s, err := execute(ctx, os.Args[0], []string{"--hang-child"}, nil)
	cancel()
	if !errors.Is(err, context.DeadlineExceeded) || !s.Waited || s.PID <= 0 || s.ExitCode == 0 {
		t.Fatalf("hung test child not reaped: %+v %v", s, err)
	}
	for _, arg := range []string{"--failed-child", "--output-flood", "--fake-success"} {
		t.Run(arg, func(t *testing.T) {
			r, s, err := call(context.Background(), os.Args[0], []string{arg}, []byte(`{}`))
			if err == nil || r.Accepted || !s.Waited {
				t.Fatalf("accepted child failure: %+v %+v %v", r, s, err)
			}
		})
	}
	_, s, err = execute(context.Background(), "/nonexistent/canonical-worker", nil, nil)
	if err == nil || s.PID != 0 || s.Waited {
		t.Fatalf("bad start state: %+v %v", s, err)
	}
	// Cancellation while queued must not start a process or consume another slot.
	for i := 0; i < cap(slots); i++ {
		slots <- struct{}{}
	}
	ctx, cancel = context.WithTimeout(context.Background(), 20*time.Millisecond)
	_, s, err = execute(ctx, os.Args[0], []string{"--failed-child"}, nil)
	cancel()
	for i := 0; i < cap(slots); i++ {
		<-slots
	}
	if !errors.Is(err, context.DeadlineExceeded) || s.PID != 0 || s.Waited {
		t.Fatalf("queued cancellation started child: %+v %v", s, err)
	}
	if len(slots) != 0 {
		t.Fatal("slot leak")
	}
}

func TestCanonicalInputBounds(t *testing.T) {
	// Start with the complete authenticated fixture/canonical candidate, changing
	// exactly one envelope property per case. No missing-fixture fallback can
	// accidentally make a broken schema check appear to pass.
	request, _ := fixture(t)
	valid, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	unknown := append([]byte(`{"unexpected":true,`), valid[1:]...)
	duplicate := append([]byte(`{"fixture_id":"injected",`), valid[1:]...)
	trailing := append(bytes.Clone(valid), []byte(` {}`)...)
	oversized := append(bytes.Clone(valid), bytes.Repeat([]byte{' '}, protocol.MaxInput+1-len(valid))...)
	cases := []struct {
		name string
		raw  []byte
		want string
	}{
		{"oversize", oversized, "input exceeds 4 MiB"},
		{"trailing", trailing, "exactly one JSON value required"},
		{"unknown", unknown, `json: unknown field "unexpected"`},
		{"duplicate", duplicate, "duplicate or invalid JSON key"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, s, err := call(context.Background(), worker(t), nil, tc.raw)
			if err == nil || r.Accepted || r.Stage != "admission" || r.Error != tc.want || r.Reads != 0 || r.Misses != 0 || !s.Waited || s.ExitCode != 2 {
				t.Fatalf("wrong admission failure: %+v %+v %v", r, s, err)
			}
			t.Logf("exact admission error=%q; bytes=%d PID=%d waited=%v exit=%d elapsed=%s (Start through Wait; excludes slot wait)", r.Error, len(tc.raw), s.PID, s.Waited, s.ExitCode, s.Elapsed)
		})
	}
}

func TestSupervisorDependencyBoundary(t *testing.T) {
	cmd := exec.Command("go", "list", "-deps", "-test", "-tags=canonicalisolation", ".")
	graph, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("dependency graph: %v %s", err, graph)
	}
	forbidden := []string{"github.com/ethereum/go-ethereum/", "github.com/bnb-chain/bsc", "github.com/prysmaticlabs/prysm/", "github.com/supranational/blst", "github.com/herumi/"}
	for _, name := range forbidden {
		if bytes.Contains(graph, []byte(name)) {
			t.Fatalf("supervisor links forbidden dependency %s", name)
		}
	}
	info, err := buildinfo.ReadFile(os.Args[0])
	if err != nil {
		t.Fatal(err)
	}
	metadata := []byte(info.String())
	for _, name := range forbidden {
		if bytes.Contains(metadata, []byte(strings.TrimSuffix(name, "/"))) {
			t.Fatalf("supervisor build metadata links %s", name)
		}
	}
	symbols, err := exec.Command("go", "tool", "nm", os.Args[0]).CombinedOutput()
	if err != nil {
		t.Fatalf("supervisor symbols: %v %s", err, symbols)
	}
	for _, name := range []string{"go-ethereum/", "prysmaticlabs/", "blst_", "herumi"} {
		if bytes.Contains(symbols, []byte(name)) {
			t.Fatalf("supervisor has forbidden symbol %s", name)
		}
	}
	if dir := os.Getenv("BSC_CANONICAL_EVIDENCE_DIR"); dir != "" {
		for name, data := range map[string][]byte{"supervisor-deps.txt": graph, "supervisor-buildinfo.txt": metadata, "supervisor-nm.txt": symbols} {
			path := filepath.Join(dir, name)
			f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if err != nil {
				t.Fatal(err)
			}
			_, err = f.Write(data)
			closeErr := f.Close()
			if err != nil || closeErr != nil {
				t.Fatalf("evidence write: %v %v", err, closeErr)
			}
		}
	}
	t.Log("supervisor dependency graph, binary build metadata and nm symbols exclude BSC/Prysm/blst/Herumi")
}
