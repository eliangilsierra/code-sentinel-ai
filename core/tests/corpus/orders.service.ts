import { HttpClient } from '@angular/common/http';

@Injectable({ providedIn: 'root' })
export class OrdersService {
  private cache = new Map<string, Order>(); /*?none*/

  /*<ctor*/ constructor(private readonly http: HttpClient, private readonly log: Logger) {} /*>ctor*/

  /*<load*/ async load(id: string): Promise<Map<string, Order>> {
    const cached = this.cache.get(id);
    if (cached) {
      return new Map([[id, cached]]); /*?load*/
    }
    const order = await this.http.get<Order>(`/api/orders/${id}`).toPromise();
    this.cache.set(id, order);
    return this.cache;
  } /*>load*/

  /*<size*/ get size(): number {
    const keys = Array.from(this.cache.keys());
    const count = keys.length;
    return count; /*?size*/
  } /*>size*/

  /*<resize*/ @HostListener('window:resize', ['$event'])
  onResize(event: UIEvent) {
    const width = (event.target as Window).innerWidth;
    this.log.debug(`width ${width}`);
    this.width = width; /*?resize*/
  } /*>resize*/

  /*<create*/ static create<T extends Order>(input: Partial<T>, defaults: T): T {
    const merged = { ...defaults, ...input };
    validate(merged);
    audit(merged);
    return merged as T; /*?create*/
  } /*>create*/

  /*<clear*/ clear() {
    this.cache.clear(); /*?clear*/
  } /*>clear*/
}

export const handlers = {
  /*<create2*/ create(req: Request, res: Response) {
    const body = req.body;
    validate(body);
    res.json(store.add(body)); /*?create2*/
  }, /*>create2*/
  /*<remove*/ remove: async (req: Request, res: Response) => {
    await store.delete(req.params.id);
    log('removed');
    audit('removed');
    res.status(204).end(); /*?remove*/
  }, /*>remove*/
};

export default {
  methods: {
    /*<save*/ save() {
      const payload = this.form;
      this.validate(payload);
      this.$emit('save', payload);
      this.saved = true; /*?save*/
    }, /*>save*/
  },
};

export function overloaded(value: string): number;
export function overloaded(value: number): number;
/*<overloaded*/ export function overloaded(value: string | number): number {
  const parsed = typeof value === 'string' ? Number(value) : value;
  const rounded = Math.round(parsed);
  return rounded; /*?overloaded*/
} /*>overloaded*/

const total = (items: number[]) => items.reduce((a, b) => a + b, 0); /*?none*/

/*<boot*/ (function boot() {
  const started = Date.now();
  const config = load();
  register(config);
  console.log(started); /*?boot*/
})(); /*>boot*/
