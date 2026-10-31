import { Dog } from "./animals";
import { Database } from "./storage";

export function run(): void {
  const d = new Dog();
  d.speak();
  d.save(); // -> Dog#save, NOT Database#save

  const db = new Database();
  db.save(); // -> Database#save, NOT Dog#save
}
