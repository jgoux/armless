// A small HTTP server for the Compute e2e: every request to /hit (or to anything but /healthz, /now and /rand)
// inserts a row into hits over DATABASE_URL and answers with its id; /healthz answers once the
// database is reachable (without inserting); /now answers with the guest's Unix time in milliseconds,
// without the database (the snapshot test runs the image without one); /rand answers with 16 bytes from the
// kernel's random generator, in hex (a restored guest must not repeat another's). With WARM_DB=1 it also opens a
// connection to the database before it listens, as an app that connects at startup does: a snapshot of
// it holds a connection that is dead once restored. With LOAD_MB=n it first writes a file of n MiB and starts a
// goroutine that is always runnable (compute/host/tests/snapshot.rs: a snapshot of a machine whose filesystem
// holds written data, and which never idles).
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"fmt"
	"log"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const schema = `create table if not exists hits (
	id bigserial primary key,
	at timestamptz default now()
)`

type app struct {
	pool *pgxpool.Pool

	mu    sync.Mutex
	ready bool
}

// ensure makes the table once the database answers.
func (a *app) ensure(ctx context.Context) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.ready {
		return nil
	}
	if _, err := a.pool.Exec(ctx, schema); err != nil {
		return err
	}
	a.ready = true
	return nil
}

func (a *app) healthz(w http.ResponseWriter, r *http.Request) {
	if err := a.ensure(r.Context()); err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	fmt.Fprintln(w, "ok")
}

func (a *app) now(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintln(w, time.Now().UnixMilli())
}

func (a *app) rand(w http.ResponseWriter, r *http.Request) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	fmt.Fprintf(w, "%x\n", b)
}

func (a *app) hit(w http.ResponseWriter, r *http.Request) {
	if err := a.ensure(r.Context()); err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	// A connection that broke (a restored snapshot has none that work) is closed by pgx and
	// dropped by the pool on error, so trying again opens a new one. The pool holds at most two.
	// A dead connection may only be found out by its timeout.
	var id int64
	var err error
	for attempt := 0; attempt < 3; attempt++ {
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		err = a.pool.QueryRow(ctx, "insert into hits default values returning id").Scan(&id)
		cancel()
		if err == nil {
			fmt.Fprintf(w, "%d\n", id)
			return
		}
	}
	http.Error(w, err.Error(), http.StatusInternalServerError)
}

// warm waits for the database and leaves a connection to it in the pool.
func (a *app) warm() {
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		err := a.ensure(ctx)
		if err == nil {
			err = a.pool.Ping(ctx)
		}
		cancel()
		if err == nil {
			return
		}
		log.Printf("warm: %v", err)
		time.Sleep(time.Second)
	}
}

// load writes a file of mb MiB, flushed to the filesystem, and starts a goroutine that always has work: the
// guest never idles.
func load(mb int) {
	f, err := os.Create(os.TempDir() + "/load.bin")
	if err != nil {
		log.Fatal(err)
	}
	chunk := bytes.Repeat([]byte{0xa5}, 1<<20)
	for i := 0; i < mb; i++ {
		if _, err := f.Write(chunk); err != nil {
			log.Fatal(err)
		}
	}
	if err := f.Sync(); err != nil {
		log.Fatal(err)
	}
	if err := f.Close(); err != nil {
		log.Fatal(err)
	}
	go func() {
		for {
			runtime.Gosched()
		}
	}()
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	cfg, err := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	cfg.MaxConns = 2
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		log.Fatal(err)
	}
	a := &app{pool: pool}
	if mb, _ := strconv.Atoi(os.Getenv("LOAD_MB")); mb > 0 {
		load(mb)
	}
	if os.Getenv("WARM_DB") == "1" {
		a.warm()
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", a.healthz)
	mux.HandleFunc("/now", a.now)
	mux.HandleFunc("/rand", a.rand)
	mux.HandleFunc("/hit", a.hit)
	mux.HandleFunc("/", a.hit)
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("listening on :%s", port)
	log.Fatal(srv.ListenAndServe())
}
