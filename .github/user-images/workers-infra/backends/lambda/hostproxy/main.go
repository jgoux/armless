// The in-guest host-restoring proxy for user-Dockerfile images. Catalog
// runtimes restore the tenant's public hostname in the shim, but a Dockerfile
// worker's server is the first thing to see the request — so this sits on the
// serving port, rewrites Host from X-Forwarded-Host, and hands the request to
// the app on a loopback port. Static Go because a user image guarantees no
// interpreter and no libc, same constraint as the launcher; the launcher
// starts it and points the app's PORT past it.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

// appHost is the Host the app should see: the hostname the tenant's client
// addressed, carried on X-Forwarded-Host past the platform's Host-routed
// hops; absent that, the Host that arrived.
func appHost(in *http.Request) string {
	if v := in.Header.Get("X-Forwarded-Host"); v != "" {
		return strings.TrimSpace(strings.Split(v, ",")[0])
	}
	return in.Host
}

func rewrite(pr *httputil.ProxyRequest, target *url.URL) {
	pr.SetURL(target)
	pr.Out.Host = appHost(pr.In)
	// Rewrite mode drops inbound X-Forwarded-*; keep the forwarding headers
	// visible so the app sees the same set the shim runtimes do.
	for _, h := range []string{"X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-For"} {
		if v := pr.In.Header.Get(h); v != "" {
			pr.Out.Header.Set(h, v)
		}
	}
}

// serve runs the proxy until stop fires, then drains: no new connections,
// in-flight requests — a stream the platform is holding open across a
// terminate — run to completion, bounded by drainTimeout. Returning early
// would exit the process and cut them; the launcher answers the terminate
// hook only once the app exits, and the app waits on these connections.
func serve(listen string, target *url.URL, stop <-chan struct{}, drainTimeout time.Duration) error {
	server := &http.Server{
		Addr: listen,
		Handler: &httputil.ReverseProxy{
			Rewrite:       func(pr *httputil.ProxyRequest) { rewrite(pr, target) },
			FlushInterval: -1,
			ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
				http.Error(w, "compute instance: app unreachable", http.StatusBadGateway)
			},
		},
	}
	drained := make(chan error, 1)
	go func() {
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), drainTimeout)
		defer cancel()
		drained <- server.Shutdown(ctx)
	}()
	if err := server.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return <-drained
}

func main() {
	listen := flag.String("listen", ":8080", "address to serve on")
	targetAddr := flag.String("target", "127.0.0.1:8081", "app address to proxy to")
	flag.Parse()
	target := &url.URL{Scheme: "http", Host: *targetAddr}

	stop := make(chan struct{})
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-signals
		close(stop)
	}()

	log.Printf("compute proxy: %s -> %s", *listen, *targetAddr)
	if err := serve(*listen, target, stop, 20*time.Second); err != nil {
		log.Fatalf("compute proxy: %v", err)
	}
}
