// cabundle rewrites a PEM CA bundle without the roots this repo distrusts.
//
// Every runtime image passes its own trust store through this at build time
// (deploy/*.Dockerfile, backends/lambda/images/*/Dockerfile). The base images'
// bundles trail the root programs by weeks, and Go and rustls check no
// revocation, so a root the programs have pulled stays trusted until the
// bundle a client reads actually changes. Roots are pinned by the SHA-256 of
// their DER, never by subject, so nothing is dropped by name collision. A
// pinned root already absent from the input is not an error — that is the
// upstream bundle catching up.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
)

// distrusted maps SHA-256(DER) to the subject it belongs to, for the log line.
var distrusted = map[string]string{
	// eMudhra, October 2026: eight TLS intermediates under this root were
	// issued with recoverable private keys. Removed by Mozilla, Chrome and
	// webpki-roots 1.0.10.
	"86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
	// The same intermediates are cross-signed by this Sectigo root. Mozilla
	// and Chrome dropped its TLS trust in April 2025 (older than fifteen
	// years) and the alpine bundles no longer carry it; Debian bookworm's,
	// which distroless ships, still does.
	"d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": "AAA Certificate Services",
}

func main() {
	log.SetFlags(0)
	in := flag.String("in", "", "PEM bundle to read")
	out := flag.String("out", "", "where to write the filtered bundle (default: rewrite -in in place)")
	flag.Parse()
	if *in == "" {
		log.Fatal("cabundle: -in is required")
	}
	if *out == "" {
		*out = *in
	}
	src, err := os.ReadFile(*in)
	if err != nil {
		log.Fatalf("cabundle: %v", err)
	}
	dst, removed, kept, err := filter(src, distrusted)
	if err != nil {
		log.Fatalf("cabundle: %s: %v", *in, err)
	}
	if err := os.WriteFile(*out, dst, 0o644); err != nil {
		log.Fatalf("cabundle: %v", err)
	}
	for _, name := range removed {
		log.Printf("cabundle: removed %q", name)
	}
	log.Printf("cabundle: %d certificates kept, %d removed -> %s", kept, len(removed), *out)
}

// filter re-emits every PEM block of src except the certificates whose DER
// hashes to a distrusted entry. Non-certificate blocks pass through untouched;
// text between blocks (the per-certificate comments some distributions write)
// is dropped, which every consumer tolerates.
func filter(src []byte, distrusted map[string]string) (dst []byte, removed []string, kept int, err error) {
	rest := src
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			break
		}
		if block.Type == "CERTIFICATE" {
			sum := sha256.Sum256(block.Bytes)
			if name, ok := distrusted[hex.EncodeToString(sum[:])]; ok {
				removed = append(removed, name)
				continue
			}
			kept++
		}
		dst = append(dst, pem.EncodeToMemory(block)...)
	}
	if kept == 0 {
		return nil, nil, 0, errors.New("no certificates left in bundle")
	}
	return dst, removed, kept, nil
}

func init() {
	for fp := range distrusted {
		if _, err := hex.DecodeString(fp); err != nil || len(fp) != sha256.Size*2 {
			panic(fmt.Sprintf("cabundle: malformed fingerprint %q", fp))
		}
	}
}
