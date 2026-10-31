export class Database {
  // Same method name as Dog.save in animals.ts. A name-based resolver
  // sees two `save` definitions and cannot disambiguate the call sites;
  // SCIP resolves each call to exactly one of these.
  save(): void {
    // persist a record
  }
}
