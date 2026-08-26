package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"testing"
)

func TestAtomicSavePathsDelegatePostReplaceDirectoryDurability(t *testing.T) {
	for _, name := range []string{"auth.go", "cursor.go"} {
		t.Run(name, func(t *testing.T) {
			source, err := os.ReadFile(name)
			if err != nil {
				t.Fatal(err)
			}
			file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
			if err != nil {
				t.Fatal(err)
			}
			var callsSyncParentDirectory bool
			ast.Inspect(file, func(node ast.Node) bool {
				call, ok := node.(*ast.CallExpr)
				if !ok {
					return true
				}
				function, ok := call.Fun.(*ast.Ident)
				if ok && function.Name == "syncParentDirectory" {
					callsSyncParentDirectory = true
				}
				return true
			})
			if !callsSyncParentDirectory {
				t.Fatal("atomic save does not delegate directory durability to syncParentDirectory")
			}
		})
	}
}
