export interface Animal {
  speak(): string;
}

export class Dog implements Animal {
  speak(): string {
    return "woof";
  }

  // Same method name as Database.save in storage.ts — this is the
  // collision that defeats name-based (tree-sitter) call resolution.
  save(): void {
    // persist the dog
  }
}

export class Cat implements Animal {
  speak(): string {
    return "meow";
  }
}
