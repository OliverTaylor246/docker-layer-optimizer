package main

import (
	"os"

	"github.com/wendylabsinc/docker-layer-optimizer/internal/cli"
)

func main() {
	os.Exit(cli.Execute(os.Args[1:], os.Stdout, os.Stderr))
}
