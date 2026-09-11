package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:11435", "loopback address for all Ollama clients")
	upstream := flag.String("upstream", "http://127.0.0.1:11434", "existing Ollama base URL")
	otlp := flag.String("otlp-http", "http://127.0.0.1:4318", "loopback OTLP/HTTP base URL")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	exporter, err := newExporter(*otlp, logger)
	if err != nil {
		logger.Error("invalid telemetry endpoint", "error", err)
		os.Exit(2)
	}
	defer exporter.Close()

	handler, err := newProxy(*listen, *upstream, exporter, logger)
	if err != nil {
		logger.Error("invalid proxy configuration", "error", err)
		os.Exit(2)
	}
	server := &http.Server{
		Addr:              *listen,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       90 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			logger.Error("proxy shutdown failed", "error", err)
		}
	}()

	logger.Info("Ollama gateway listening", "listen", *listen, "upstream", *upstream)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Error("proxy stopped unexpectedly", "error", err)
		os.Exit(1)
	}
	fmt.Fprintln(os.Stderr, "Ollama gateway stopped")
}
